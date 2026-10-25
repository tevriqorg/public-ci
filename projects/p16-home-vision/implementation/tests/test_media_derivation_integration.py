from __future__ import annotations

import json
import hashlib
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from media_derivation.pipeline import DerivationPaths, DerivationPlan, run_derivation
from media_derivation.probe import probe_source
from media_derivation.cli import main as media_derivation_main


def _files_containing(root: Path, token: bytes) -> list[str]:
    return [str(path.relative_to(root)) for path in root.rglob('*')
            if path.is_file() and token in path.read_bytes()]


def _make_hevc_sample(path: Path, *, required: bool = False) -> None:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=10:duration=2.4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=8000:duration=2.4",
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-x265-params",
            "pools=1:frame-threads=1:log-level=error",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "pcm_alaw",
            "-shortest",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = f"ffmpeg cannot create required HEVC fixture: {completed.stderr[-500:]}"
        if required:
            pytest.fail(detail)
        pytest.skip(detail)


def _probe(path: Path) -> dict:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _assert_real_hevc_derivation(tmp_path: Path, *, require_encoder: bool):
    raw = tmp_path / "source.mkv"
    _make_hevc_sample(raw, required=require_encoder)
    source = probe_source(raw)
    assert source.video_codec == "hevc"
    assert source.has_audio is True

    paths = DerivationPaths(raw=raw, root=tmp_path / "asset")
    result = run_derivation(
        paths,
        DerivationPlan(has_audio=True),
        source_start_epoch=1_700_000_000.0,
    )

    assert raw.is_file(), "derivation must never delete RAW"
    assert paths.web_final.is_file()
    assert paths.audio_final.is_file()
    assert paths.frame_manifest_final.is_file()
    assert result["raw_deleted"] is False
    assert int(result["frame_count"]) >= 2

    web_streams = _probe(paths.web_final)["streams"]
    video = next(stream for stream in web_streams if stream["codec_type"] == "video")
    audio = next(stream for stream in web_streams if stream["codec_type"] == "audio")
    assert video["codec_name"] == "hevc"
    assert video.get("codec_tag_string") == "hvc1"
    assert audio["codec_name"] == "aac"

    frame_rows = [
        json.loads(line)
        for line in paths.frame_manifest_final.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(frame_rows) == result["frame_count"]
    source_pts = [row["source_pts_seconds"] for row in frame_rows]
    absolute_times = [row["absolute_epoch_seconds"] for row in frame_rows]
    assert all(isinstance(value, (int, float)) for value in source_pts + absolute_times)
    assert source_pts == sorted(source_pts) and source_pts[0] >= 0
    assert all(abs(absolute - (1_700_000_000.0 + pts)) <= 0.01
               for pts, absolute in zip(source_pts, absolute_times))


def test_real_derivation_keeps_raw_and_outputs_web_frames_audio(tmp_path: Path):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe required")
    _assert_real_hevc_derivation(tmp_path, require_encoder=False)


def test_s019_required_hevc_pcm_stream_copy_preserves_pts(tmp_path: Path):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.fail("S019 requires ffmpeg/ffprobe; scenario cannot be a skip")
    _assert_real_hevc_derivation(tmp_path, require_encoder=True)


def test_s027_synthetic_raw_retention_and_secret_redaction(tmp_path: Path, monkeypatch, capsys):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.fail("S027 requires ffmpeg/ffprobe; scenario cannot be a skip")
    raw = tmp_path / "source-synthetic.mkv"
    _make_hevc_sample(raw, required=True)
    before = hashlib.sha256(raw.read_bytes()).hexdigest()
    canary = secrets.token_hex(24).encode()
    asset_root = tmp_path / "assets"
    monkeypatch.setenv("P16_RAW_RETENTION", "0")
    monkeypatch.setenv("P16_CLEANUP_RAW", "1")
    monkeypatch.setenv("P16_NVR_PASSWORD", canary.decode())
    monkeypatch.setenv("P16_NVR_ENDPOINT", "http://198.51.100.23:8000")
    monkeypatch.setenv("P16_HTTP_BIND_HOST", "0.0.0.0")
    monkeypatch.setattr(sys, "argv", [
        "p16-media-derive", "--raw", str(raw), "--asset-root", str(asset_root),
        "--asset-id", "synthetic-s027", "--camera", "synthetic-canary",
        "--source-start-epoch", "1792872000",
    ])

    assert media_derivation_main() == 0
    captured = capsys.readouterr()
    assert canary.decode() not in captured.out
    assert canary.decode() not in captured.err
    assert raw.is_file()
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == before
    status_path = asset_root / "synthetic-s027" / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["state"] == "derived"
    assert status["raw_deleted"] is False
    assert status["stages"]["raw"] == "done"
    assert _files_containing(asset_root, canary) == []

    # Mutation canary: prove the independent persisted-file scanner detects an
    # accidental secret leak before trusting its clean result above.
    injected = asset_root / "synthetic-secret-leak-canary.txt"
    injected.write_bytes(b"credential=" + canary)
    try:
        self_report = _files_containing(asset_root, canary)
        assert self_report == ["synthetic-secret-leak-canary.txt"]
    finally:
        injected.unlink(missing_ok=True)
    assert _files_containing(asset_root, canary) == []
