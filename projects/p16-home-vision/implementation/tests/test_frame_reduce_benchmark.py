from dataclasses import replace
import json
from pathlib import Path

import pytest

from app.domain.frame_reduce import SelectedFrame, WorkingFrame, load_working_frame_manifest
from app.domain.frame_reduce_benchmark import benchmark_reducer, benchmark_report


class EveryOtherFrame:
    def reduce(self, frames):
        return tuple(SelectedFrame(frame, ("fixture",), reducer_id="every_other", reducer_version="1") for frame in frames[::2])


def test_benchmark_reports_reference_recall_and_reduction():
    frames = tuple(WorkingFrame(f"f{i}", "asset", f"{i}.jpg", float(i), i * 1000) for i in range(10))
    result = benchmark_reducer(EveryOtherFrame(), frames, (0.0, 4.0, 10.0), tolerance_seconds=1.0)
    assert result.selected_frames == 5
    assert result.reduction_ratio == 0.5
    assert result.reference_recall == pytest.approx(2 / 3)
    assert benchmark_report([result], dataset_id="synthetic-fixture", dataset_revision="v1")["schema"] == "p16.frame_reduce_benchmark.v1"


class MutatingFrameReducer:
    def reduce(self, frames):
        return (SelectedFrame(replace(frames[0], source_pts_seconds=frames[0].source_pts_seconds + 99), ("mutated",)),)


def test_benchmark_rejects_selected_frame_with_forged_identity():
    frames = (WorkingFrame("f0", "asset", "0.jpg", 0.0, 0),)
    with pytest.raises(ValueError, match="FRAME_BENCHMARK_MUTATED_FRAME"):
        benchmark_reducer(MutatingFrameReducer(), frames, (0.0,))


def test_s022_variable_pts_static_and_motion_manifest_has_grounded_selection(tmp_path: Path):
    # Synthetic luminance labels represent long static spans and two motion
    # transitions. Deliberately unrelated frame indexes catch index/FPS timing.
    rows = [
        (100, "00000100.ppm", 0.0, 24),
        (101, "00000101.ppm", 0.37, 24),
        (104, "00000104.ppm", 0.91, 24),
        (109, "00000109.ppm", 1.48, 220),
        (117, "00000117.ppm", 2.23, 220),
        (130, "00000130.ppm", 3.06, 220),
        (149, "00000149.ppm", 4.41, 32),
        (173, "00000173.ppm", 5.02, 32),
    ]
    manifest = tmp_path / "working-frames.jsonl"
    manifest.write_text(
        "\n".join(json.dumps({
            "index": index,
            "file": name,
            "source_pts_seconds": pts,
            "synthetic_luminance": luminance,
        }) for index, name, pts, luminance in rows) + "\n",
        encoding="utf-8",
    )
    frames = load_working_frame_manifest(manifest, asset_id="s022-asset")
    luminance_by_id = {
        f"s022-asset:frame:{index}": luminance
        for index, _, _, luminance in rows
    }

    class SyntheticMotionReducer:
        def reduce(self, candidates):
            output = [SelectedFrame(
                candidates[0], ("initial_context",), (("change", 0.0),),
                "synthetic-motion", "fixture-v1",
            )]
            for previous, current in zip(candidates, candidates[1:]):
                change = abs(luminance_by_id[current.artifact_id] - luminance_by_id[previous.artifact_id])
                if change >= 100:
                    output.append(SelectedFrame(
                        current, ("motion_change",), (("change", float(change)),),
                        "synthetic-motion", "fixture-v1",
                    ))
            return tuple(output)

    reducer = SyntheticMotionReducer()
    selected = reducer.reduce(frames)
    expected = (
        ("s022-asset:frame:100", 0.0),
        ("s022-asset:frame:109", 1.48),
        ("s022-asset:frame:149", 4.41),
    )
    assert tuple((row.frame.artifact_id, row.frame.source_pts_seconds) for row in selected) == expected
    assert tuple(row.frame.source_offset_ms for row in selected) == (0, 1480, 4410)

    metrics = benchmark_reducer(reducer, frames, (0.0, 1.48, 4.41), tolerance_seconds=0.01)
    assert metrics.input_frames == 8
    assert metrics.selected_frames == 3
    assert metrics.reduction_ratio == 0.375
    assert metrics.reference_recall == 1.0
    assert metrics.mean_reference_error_seconds == 0.0
