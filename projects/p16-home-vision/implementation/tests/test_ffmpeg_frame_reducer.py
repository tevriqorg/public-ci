from __future__ import annotations

import json
import subprocess
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


def test_candidate_reducer_uses_source_pts_from_non_integer_vfr_video(tmp_path: Path):
    """Exercise the production reducer with ffprobe-timed variable-rate frames."""
    source = tmp_path / "synthetic-vfr.mp4"
    frame_root = tmp_path / "working-frames"
    frame_root.mkdir()
    source_indices = (0, 11, 27, 30, 67, 69, 130, 151)
    pts_by_output = (0, 0.37, 0.91, 1.48, 2.23, 3.06, 4.41, 5.02)
    pts_expression = "if(eq(N\\,0)\\,0"
    for position, timestamp in enumerate(pts_by_output[1:], start=1):
        pts_expression += f"\\,if(eq(N\\,{position})\\,{timestamp}/TB"
    pts_expression += "\\,5.02/TB" + ")" * len(pts_by_output)
    filters = (
        "drawbox=x=8:y=8:w=48:h=48:color=white:t=fill:"
        "enable='gte(t\\,1)*lt(t\\,3)',"
        "select='" + "+".join(f"eq(n\\,{index})" for index in source_indices) + "',"
        f"setpts='{pts_expression}'"
    )
    generated = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:r=30000/1001:d=6",
            "-vf", filters, "-fps_mode", "vfr", "-c:v", "libx264",
            "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-bf", "0",
            "-enc_time_base", "1/1000", "-video_track_timescale", "90000",
            str(source),
        ], capture_output=True, check=False, timeout=120,
    )
    assert generated.returncode == 0, generated.stderr.decode(errors="replace")[-1000:]

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate,avg_frame_rate",
            "-show_entries", "frame=best_effort_timestamp_time", "-of", "json",
            str(source),
        ], capture_output=True, check=True, text=True, timeout=30,
    )
    probe_data = json.loads(probe.stdout)
    stream = probe_data["streams"][0]
    actual_pts = tuple(float(row["best_effort_timestamp_time"])
                       for row in probe_data["frames"])
    assert stream["r_frame_rate"] == "30000/1001"
    assert len(actual_pts) == len(source_indices)
    intervals = tuple(round(right - left, 3)
                      for left, right in zip(actual_pts, actual_pts[1:]))
    assert len(set(intervals)) > 1

    extracted = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-fps_mode", "vfr", str(frame_root / "frame-%02d.ppm"),
        ], capture_output=True, check=False, timeout=60,
    )
    assert extracted.returncode == 0, extracted.stderr.decode(errors="replace")[-1000:]
    image_paths = sorted(frame_root.glob("frame-*.ppm"))
    assert len(image_paths) == len(actual_pts)

    def mean_rgb(path: Path) -> float:
        image = path.read_bytes()
        header_end = image.index(b"\n255\n") + len(b"\n255\n")
        pixels = image[header_end:]
        assert len(pixels) == 64 * 64 * 3
        return sum(pixels) / len(pixels)

    # The independently generated visual oracle is static black, white motion,
    # then static black; it does not reuse the reducer's FFmpeg pixel scores.
    means = tuple(mean_rgb(path) for path in image_paths)
    assert all(value < 5 for value in means[:3])
    assert all(value > 80 for value in means[3:6])
    assert all(value < 5 for value in means[6:])

    manifest = tmp_path / "vfr-working-frames.jsonl"
    manifest.write_text("".join(json.dumps({
        "index": source_indices[position] + 1,
        "file": image_paths[position].name,
        "source_pts_seconds": actual_pts[position],
    }) + "\n" for position in range(len(image_paths))), encoding="utf-8")
    frames = load_working_frame_manifest(manifest, asset_id="s022-vfr-asset")
    selected = FfmpegLumaDifferenceReducer(
        frame_root, change_threshold=0.1, width=16, height=16,
    ).reduce(frames)

    # Compare against source frame ordinals and ffprobe PTS, never a nominal-FPS
    # reconstruction from the sparse working-frame positions.
    assert tuple(row.frame.artifact_id for row in selected) == (
        "s022-vfr-asset:frame:1",
        "s022-vfr-asset:frame:31",
        "s022-vfr-asset:frame:131",
    )
    assert tuple(row.frame.source_pts_seconds for row in selected) == (
        actual_pts[0], actual_pts[3], actual_pts[6],
    )
    nominal_fps_offsets = tuple(index / (30000 / 1001) for index in (0, 30, 130))
    assert actual_pts[3] != pytest.approx(nominal_fps_offsets[1], abs=0.01)
    assert actual_pts[6] != pytest.approx(nominal_fps_offsets[2], abs=0.01)
    metrics = benchmark_reducer(
        FfmpegLumaDifferenceReducer(frame_root, change_threshold=0.1, width=16, height=16),
        frames, (actual_pts[0], actual_pts[3], actual_pts[6]), tolerance_seconds=0.001,
    )
    assert metrics.input_frames == 8
    assert metrics.selected_frames == 3
    assert metrics.reference_recall == 1.0


def _make_manifest(root: Path, file_name: str) -> Path:
    manifest = root / "unsafe-frames.jsonl"
    manifest.write_text(json.dumps({
        "index": 1, "file": file_name, "source_pts_seconds": 0.0,
    }) + "\n", encoding="utf-8")
    return manifest
