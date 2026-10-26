from __future__ import annotations

import json
import hashlib
import resource
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from media_derivation.pipeline import DerivationPaths, DerivationPlan, run_derivation
from media_derivation.probe import probe_source
from media_derivation.cli import main as media_derivation_main


def _files_containing(root: Path, token: bytes) -> list[str]:
    return [str(path.relative_to(root)) for path in root.rglob('*')
            if path.is_file() and token in path.read_bytes()]


def _make_hevc_sample(path: Path, *, required: bool = False, duration_seconds: float = 2.4) -> None:
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
            f"testsrc2=size=160x90:rate=10:duration={duration_seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=880:sample_rate=8000:duration={duration_seconds}",
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
        timeout=240,
    )
    if completed.returncode != 0:
        detail = f"ffmpeg cannot create required HEVC fixture: {completed.stderr[-500:]}"
        if required:
            pytest.fail(detail)
        pytest.skip(detail)


def _make_h264_aac_mp4_sample(path: Path) -> None:
    completed = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=12:duration=2.5",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=2.5",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "64k", "-shortest", "-movflags", "+faststart",
            str(path),
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=120,
    )
    if completed.returncode != 0:
        pytest.fail(f"S019 cannot create required H264/AAC MP4 fixture: {completed.stderr[-500:]}")


def _probe(path: Path) -> dict:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _video_packet_pts(path: Path) -> list[float]:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_packets",
         "-show_entries", "packet=pts_time", "-of", "json", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
    )
    return [float(packet["pts_time"]) for packet in json.loads(completed.stdout)["packets"]
            if "pts_time" in packet]


def _assert_derivation_pts_against_source(raw: Path, web: Path, manifest: Path) -> None:
    """Use ffprobe packet timestamps as an oracle independent of showinfo output."""
    source_pts = _video_packet_pts(raw)
    output_pts = _video_packet_pts(web)
    assert len(source_pts) == len(output_pts) >= 2
    assert all(abs(source - output) <= 0.02 for source, output in zip(source_pts, output_pts))
    rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    assert rows and rows[0]["source_pts_seconds"] >= 0
    assert rows[-1]["source_pts_seconds"] <= source_pts[-1] + 0.02
    assert all(any(abs(row["source_pts_seconds"] - pts) <= 0.02 for pts in source_pts)
               for row in rows)


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


def test_component_hevc_pcm_stream_copy_preserves_pts(tmp_path: Path):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.fail("S019 requires ffmpeg/ffprobe; scenario cannot be a skip")
    _assert_real_hevc_derivation(tmp_path, require_encoder=True)


def test_s019_multi_container_codec_derivation_preserves_source_pts(tmp_path: Path):
    """Exercise MKV/HEVC/PCM and MP4/H264/AAC through the production pipeline."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.fail("S019 requires ffmpeg/ffprobe; scenario cannot be a skip")

    hevc_raw = tmp_path / "source-hevc-pcm.mkv"
    _make_hevc_sample(hevc_raw, required=True)
    h264_raw = tmp_path / "source-h264-aac.mp4"
    _make_h264_aac_mp4_sample(h264_raw)

    cases = ((hevc_raw, "hevc", "aac"), (h264_raw, "h264", "aac"))
    for index, (raw, expected_video, expected_audio) in enumerate(cases):
        source = probe_source(raw)
        assert source.video_codec == expected_video
        assert source.has_audio is True
        raw_hash = hashlib.sha256(raw.read_bytes()).hexdigest()
        paths = DerivationPaths(raw=raw, root=tmp_path / f"asset-{index}")
        result = run_derivation(
            paths, DerivationPlan(has_audio=True), source_start_epoch=1_700_000_000.0 + index,
        )

        assert result["raw_deleted"] is False
        assert hashlib.sha256(raw.read_bytes()).hexdigest() == raw_hash
        assert paths.web_final.is_file() and paths.audio_final.is_file()
        assert paths.frame_manifest_final.is_file()
        streams = _probe(paths.web_final)["streams"]
        video = next(stream for stream in streams if stream["codec_type"] == "video")
        audio = next(stream for stream in streams if stream["codec_type"] == "audio")
        assert video["codec_name"] == expected_video
        assert audio["codec_name"] == expected_audio
        if expected_video == "hevc":
            assert video.get("codec_tag_string") == "hvc1"
        _assert_derivation_pts_against_source(raw, paths.web_final, paths.frame_manifest_final)


def test_component_raw_retention_and_secret_redaction(tmp_path: Path, monkeypatch, capsys):
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


def test_s035_ten_minute_derivation_and_large_audio_transcript_are_bounded(
    tmp_path: Path, monkeypatch,
):
    """Run long media and large transcript through their real local pipelines."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.fail("S035 requires ffmpeg/ffprobe; scenario cannot be a skip")
    raw = tmp_path / "synthetic-10m.mkv"
    _make_hevc_sample(raw, required=True, duration_seconds=600)
    source = probe_source(raw)
    assert source.duration_seconds is not None and source.duration_seconds >= 599

    raw_sha256 = hashlib.sha256(raw.read_bytes()).hexdigest()
    paths = DerivationPaths(raw=raw, root=tmp_path / "derived-10m")
    started = time.monotonic()
    result = run_derivation(paths, DerivationPlan(has_audio=True),
                            source_start_epoch=1_798_272_000.0)
    elapsed = time.monotonic() - started
    assert elapsed < 180
    assert result["raw_deleted"] is False
    assert result["frame_count"] >= 590
    assert paths.web_final.is_file() and paths.audio_final.is_file()
    assert paths.frame_manifest_final.is_file()
    web = probe_source(paths.web_final)
    assert web.duration_seconds is not None and web.duration_seconds >= 599
    frame_rows = [json.loads(line) for line in
                  paths.frame_manifest_final.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(frame_rows) == result["frame_count"]
    assert frame_rows[0]["source_pts_seconds"] >= 0
    assert frame_rows[-1]["source_pts_seconds"] >= 590

    # Exercise the production transcript CLI, bounded-memory VAD and JSON ASR
    # adapter over all 10 minutes of derived audio. The offline fixture backend
    # emits one valid timestamped segment per second without network access.
    asr_command = tmp_path / "synthetic-long-audio-asr.py"
    asr_command.write_text(
        "import json, sys\n"
        "regions = json.loads(sys.argv[1])['regions']\n"
        "end = max(region['end_ms'] for region in regions)\n"
        "rows = [{'start_ms': start, 'end_ms': min(start + 200, end), "
        "'text': f'segment-{start // 1000}', 'confidence': 0.9} "
        "for start in range(0, end, 1000)]\n"
        "print(json.dumps(rows))\n",
        encoding="utf-8",
    )
    status_path = paths.root / "status.json"
    status_path.write_text(json.dumps({
        "asset_id": paths.root.name,
        "state": "derived",
        "stages": {"web": "done", "frames": "done", "audio": "done",
                   "transcript": "idle"},
    }), encoding="utf-8")
    from media_derivation.transcribe_cli import main as transcribe_main
    transcript_started = time.monotonic()
    monkeypatch.setattr(sys, "argv", [
        "p16-media-transcribe", "--asset-root", str(tmp_path),
        "--asset-id", paths.root.name, "--local-asr-command", sys.executable,
        str(asr_command),
    ])
    assert transcribe_main() == 0
    transcript_elapsed = time.monotonic() - transcript_started
    assert transcript_elapsed < 60

    status = json.loads(status_path.read_text(encoding="utf-8"))
    transcript_path = paths.root / "transcript" / "transcript.json"
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    assert status["state"] == "derived"
    assert status["stages"]["transcript"] == "done"
    assert status["transcript"]["segments"] == len(transcript["segments"]) >= 590
    assert transcript["segments"][-1]["end_ms"] >= 590_000
    assert raw.is_file() and hashlib.sha256(raw.read_bytes()).hexdigest() == raw_sha256

    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    peak_bytes = peak if sys.platform == "darwin" else peak * 1024
    assert peak_bytes < 512 * 1024 * 1024
    footprint = sum(path.stat().st_size for path in tmp_path.rglob("*") if path.is_file())
    assert footprint < 256 * 1024 * 1024
