from dataclasses import replace
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from app.domain.frame_reduce import SelectedFrame, WorkingFrame, load_working_frame_manifest
from app.domain.frame_reduce_benchmark import benchmark_reducer, benchmark_report
from app.domain.ffmpeg_frame_reducer import FfmpegLumaDifferenceReducer
from media_derivation.pipeline import DerivationPaths, DerivationPlan, run_derivation


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


def test_component_variable_pts_static_and_motion_manifest_has_grounded_selection(tmp_path: Path):
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


def _make_s022_vfr_video(path: Path) -> None:
    completed = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=96x64:r=30000/1001:d=7",
            "-vf",
            "drawbox=x=8:y=8:w=32:h=32:color=white:t=fill:enable='between(t,2,3)',"
            "drawbox=x=48:y=8:w=32:h=32:color=white:t=fill:enable='between(t,5,6)',"
            "setpts='PTS+if(gte(N,30),10,0)+if(gte(N,60),20,0)'",
            "-fps_mode", "vfr", "-c:v", "libx264", "-threads", "1",
            "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an", str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr[-1000:]


def _s022_source_timing(path: Path) -> tuple[dict, tuple[float, ...]]:
    completed = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_streams", "-show_frames",
            "-show_entries", "stream=r_frame_rate,avg_frame_rate:frame=best_effort_timestamp_time",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    payload = json.loads(completed.stdout)
    stream = payload["streams"][0]
    pts = tuple(float(row["best_effort_timestamp_time"])
                for row in payload["frames"] if "best_effort_timestamp_time" in row)
    return stream, pts


def test_s022_real_frame_reducer_selects_vfr_source_pts_and_motion_transitions(tmp_path: Path):
    """Run generated VFR media through P16 derivation and the production reducer."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.fail("S022 requires ffmpeg/ffprobe; scenario cannot be a skip")

    raw = tmp_path / "s022-static-motion-vfr.mkv"
    _make_s022_vfr_video(raw)
    raw_sha = hashlib.sha256(raw.read_bytes()).hexdigest()
    source_stream, source_pts = _s022_source_timing(raw)
    assert Fraction(source_stream["r_frame_rate"]) == Fraction(30_000, 1_001)
    source_intervals = tuple(round(right - left, 3)
                             for left, right in zip(source_pts, source_pts[1:]))
    assert len(set(source_intervals)) >= 3
    assert max(source_intervals) >= 0.35

    source_start = 1_700_000_000.0
    paths = DerivationPaths(raw=raw, root=tmp_path / "s022-derived")
    derived = run_derivation(
        paths,
        DerivationPlan(has_audio=False, sample_fps=1.0),
        source_start_epoch=source_start,
    )
    assert derived["raw_deleted"] is False
    assert paths.web_final.is_file() and paths.frame_manifest_final.is_file()
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == raw_sha

    asset_id = "s022-vfr-asset"
    frames = load_working_frame_manifest(paths.frame_manifest_final, asset_id=asset_id)
    assert len(frames) == derived["frame_count"] == 7
    assert all(frame.asset_id == asset_id for frame in frames)
    assert all(abs(frame.absolute_epoch_seconds - (source_start + frame.source_pts_seconds)) < 0.01
               for frame in frames)
    assert all(any(abs(frame.source_pts_seconds - pts) <= 0.001 for pts in source_pts)
               for frame in frames)
    # A nominal-FPS/index-derived timestamp would be far from the PTS-backed
    # one-second samples for every frame after the first.
    assert any(abs(frame.source_pts_seconds - (index / float(Fraction(30_000, 1_001)))) > 0.5
               for index, frame in enumerate(frames, start=1))

    reducer = FfmpegLumaDifferenceReducer(paths.frames_dir, change_threshold=0.10)
    selected = reducer.reduce(frames)
    expected_windows = ((0.0, 0.01), (2.9, 3.5), (3.9, 4.5), (5.9, 6.5), (6.9, 7.5))
    selected_pts = tuple(row.frame.source_pts_seconds for row in selected)
    assert len(selected_pts) == len(expected_windows)
    assert all(low <= pts <= high for pts, (low, high) in zip(selected_pts, expected_windows))
    by_id = {frame.artifact_id: frame for frame in frames}
    assert all(row.frame == by_id[row.frame.artifact_id] for row in selected)
    expected_ids = tuple(
        next(frame.artifact_id for frame in frames if low <= frame.source_pts_seconds <= high)
        for low, high in expected_windows
    )
    assert tuple(row.frame.artifact_id for row in selected) == expected_ids
    assert tuple(row.selection_reasons for row in selected) == (
        ("initial_context",), ("image_difference",), ("image_difference",),
        ("image_difference",), ("image_difference",),
    )
    assert all(row.reducer_id == reducer.reducer_id and row.reducer_version == reducer.reducer_version
               for row in selected)
    assert all(dict(row.scores)["normalized_luma_change"] >= reducer.change_threshold
               for row in selected[1:])

    metrics = benchmark_reducer(
        reducer, frames, (0.0, 3.0, 4.0, 6.0, 7.0), tolerance_seconds=0.02,
    )
    assert metrics.input_frames == 7
    assert metrics.selected_frames == 5
    assert metrics.reduction_ratio == 0.714286
    assert metrics.reference_recall == 1.0
    assert metrics.mean_reference_error_seconds <= 0.01
    report = benchmark_report([metrics], dataset_id="s022-synthetic-vfr", dataset_revision="v1")
    assert report["schema"] == "p16.frame_reduce_benchmark.v1"
