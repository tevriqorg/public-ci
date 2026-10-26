from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain.ffmpeg_frame_reducer import FfmpegLumaDifferenceReducer
from app.domain.frame_reduce import load_working_frame_manifest
from app.domain.frame_reduce_benchmark import benchmark_reducer


def _write_solid_ppm(path: Path, value: int) -> bytes:
    pixels = bytes((value, value, value)) * (16 * 16)
    payload = b"P6\n16 16\n255\n" + pixels
    path.write_bytes(payload)
    return payload


def test_candidate_reducer_selects_real_image_changes_at_source_pts(tmp_path: Path):
    # The values provide two static spans around two clear scene changes.
    # Irregular source PTS ensure indexes and nominal FPS cannot stand in for time.
    samples = (
        (3, "000003.ppm", 0.0, 16),
        (4, "000004.ppm", 0.37, 16),
        (8, "000008.ppm", 0.91, 220),
        (11, "000011.ppm", 1.48, 220),
        (18, "000018.ppm", 2.23, 220),
        (27, "000027.ppm", 4.41, 8),
        (35, "000035.ppm", 5.02, 8),
    )
    manifest = tmp_path / "working-frames.jsonl"
    rows = []
    source_payloads = {}
    for index, name, pts, value in samples:
        source_payloads[name] = _write_solid_ppm(tmp_path / name, value)
        rows.append({"index": index, "file": name, "source_pts_seconds": pts})
    manifest.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    frames = load_working_frame_manifest(manifest, asset_id="s022-asset")
    reducer = FfmpegLumaDifferenceReducer(tmp_path, change_threshold=0.25)
    selected = reducer.reduce(frames)

    # This oracle comes from the generated source fixture: frame 8 and 27 are
    # the only transitions whose grayscale byte ranges cross the configured bound.
    expected_names = ("000003.ppm", "000008.ppm", "000027.ppm")
    assert tuple(row.frame.file_name for row in selected) == expected_names
    assert tuple(row.frame.artifact_id for row in selected) == (
        "s022-asset:frame:3", "s022-asset:frame:8", "s022-asset:frame:27",
    )
    assert tuple(row.frame.source_pts_seconds for row in selected) == (0.0, 0.91, 4.41)
    assert tuple(row.frame.source_offset_ms for row in selected) == (0, 910, 4410)
    assert all((tmp_path / name).read_bytes() == payload
               for name, payload in source_payloads.items())
    assert tuple(row.selection_reasons for row in selected) == (
        ("initial_context",), ("image_difference",), ("image_difference",),
    )
    assert all(row.reducer_id == "ffmpeg_luma_difference" and row.reducer_version == "1"
               for row in selected)

    metrics = benchmark_reducer(reducer, frames, (0.0, 0.91, 4.41), tolerance_seconds=0.001)
    assert metrics.input_frames == 7
    assert metrics.selected_frames == 3
    assert metrics.reduction_ratio == pytest.approx(3 / 7)
    assert metrics.reference_recall == 1.0
    assert metrics.mean_reference_error_seconds == 0.0


def test_candidate_reducer_requires_explicit_threshold_and_confines_frame_paths(tmp_path: Path):
    with pytest.raises(ValueError, match="FRAME_REDUCE_THRESHOLD_INVALID"):
        FfmpegLumaDifferenceReducer(tmp_path, change_threshold=0)

    frame = load_working_frame_manifest(
        _make_manifest(tmp_path, "../outside.ppm"), asset_id="unsafe-asset",
    )[0]
    reducer = FfmpegLumaDifferenceReducer(tmp_path, change_threshold=0.25)
    with pytest.raises(ValueError, match="FRAME_REDUCE_PATH_UNSAFE"):
        reducer.reduce((frame,))


def _make_manifest(root: Path, file_name: str) -> Path:
    manifest = root / "unsafe-frames.jsonl"
    manifest.write_text(json.dumps({
        "index": 1, "file": file_name, "source_pts_seconds": 0.0,
    }) + "\n", encoding="utf-8")
    return manifest
