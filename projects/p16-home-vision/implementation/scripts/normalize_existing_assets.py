from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.domain.media_access import resolve_media_path
from app.models import LocalAsset, SourceRecording, StorageVolume


NORMALIZED_ROLE = "normalized_playback"


def _validate_output(
    path: Path,
    *,
    expected_duration_ms: int,
    source_audio_codec: str | None,
    full_decode: bool = True,
) -> tuple[str, str | None, int]:
    inspected = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, check=False, timeout=120,
    )
    if inspected.returncode != 0:
        raise RuntimeError("NORMALIZED_FFPROBE_FAILED")
    try:
        payload = json.loads(inspected.stdout)
        streams = payload.get("streams") or []
        video = next(stream for stream in streams if stream.get("codec_type") == "video")
        audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
        format_name = (payload.get("format") or {}).get("format_name", "")
        duration_ms = round(float((payload.get("format") or {}).get("duration")) * 1000)
    except (StopIteration, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("NORMALIZED_METADATA_INVALID") from exc
    if "mp4" not in format_name or video.get("codec_name") != "hevc" or video.get("codec_tag_string") != "hvc1":
        raise RuntimeError("NORMALIZED_VIDEO_PROFILE_INVALID")
    allowed_duration_delta_ms = max(5000, min(120000, round(expected_duration_ms * 0.02)))
    if abs(duration_ms - expected_duration_ms) > allowed_duration_delta_ms:
        raise RuntimeError("NORMALIZED_DURATION_MISMATCH")
    if source_audio_codec == "pcm_alaw":
        if not audio or audio.get("codec_name") != "aac" or audio.get("sample_rate") != "8000" or audio.get("channels") != 1:
            raise RuntimeError("NORMALIZED_SOURCE_A_AUDIO_INVALID")
    elif source_audio_codec == "aac":
        if not audio or audio.get("codec_name") != "aac":
            raise RuntimeError("NORMALIZED_SOURCE_B_AUDIO_INVALID")
    elif audio and audio.get("codec_name") != "aac":
        raise RuntimeError("NORMALIZED_AUDIO_INVALID")

    if full_decode:
        decoded = subprocess.run(
            ["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-map", "0", "-f", "null", "-"],
            capture_output=True, text=True, check=False, timeout=7200,
        )
        if decoded.returncode != 0:
            raise RuntimeError("NORMALIZED_FULL_DECODE_FAILED")
        midpoint = max(0.0, expected_duration_ms / 2000)
        for offset in (0.0, midpoint, max(0.0, expected_duration_ms / 1000 - 1.0)):
            seek = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", str(offset), "-i", str(path), "-frames:v", "1", "-f", "null", "-"],
                capture_output=True, text=True, check=False, timeout=180,
            )
            if seek.returncode != 0:
                raise RuntimeError("NORMALIZED_SEEK_FAILED")
    return "mp4", "aac" if audio else None, duration_ms


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _register_normalized(asset: LocalAsset, volume: StorageVolume, final_path: Path, output_audio_codec: str | None, output_duration_ms: int) -> None:
    now = datetime.now(timezone.utc)
    normalized_id = f"normalized-{asset.id}"
    with SessionLocal() as session:
        existing = session.get(LocalAsset, normalized_id)
        if existing is None:
            existing = LocalAsset(id=normalized_id, source_recording_id=asset.source_recording_id, asset_role=NORMALIZED_ROLE)
            session.add(existing)
        existing.storage_volume_id = volume.id
        existing.relative_path = f"recordings/normalized/{asset.id}.mp4"
        existing.size_bytes = final_path.stat().st_size
        existing.sha256 = _sha256(final_path)
        existing.container = "mp4"
        existing.video_codec = asset.video_codec
        existing.audio_codec = output_audio_codec
        existing.width = asset.width
        existing.height = asset.height
        existing.media_duration_ms = output_duration_ms
        existing.verification_state = "verified"
        existing.verified_at = now
        existing.lifecycle_state = "present"
        existing.archived_at = now
        session.commit()


def _normalize_one(asset: LocalAsset, source: SourceRecording, volume: StorageVolume) -> str:
    source_path = resolve_media_path(Path(volume.mount_path_runtime), asset.relative_path)
    # Keep sidecars inside the existing writable recordings tree.  The volume
    # root itself is intentionally not writable by the application user.
    relative_path = f"recordings/normalized/{asset.id}.mp4"
    final_path = resolve_media_path(Path(volume.mount_path_runtime), relative_path)
    part_path = final_path.with_suffix(".mp4.part")
    expected_duration_ms = asset.media_duration_ms or source.duration_ms
    if not source_path.is_file():
        raise RuntimeError("SOURCE_FILE_MISSING")
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.is_file() and final_path.stat().st_size > 0:
        _container, output_audio_codec, output_duration_ms = _validate_output(final_path, expected_duration_ms=expected_duration_ms, source_audio_codec=asset.audio_codec)
        _register_normalized(asset, volume, final_path, output_audio_codec, output_duration_ms)
        return "already_present"
    if part_path.exists():
        part_path.unlink()

    command = [
        "nice", "-n", "19", "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-copyts", "-start_at_zero", "-avoid_negative_ts", "disabled",
        "-i", str(source_path),
        "-map", "0:v:0", "-c:v", "copy", "-tag:v", "hvc1",
        "-map", "0:a:0?",
    ]
    if (asset.audio_codec or "").lower() == "pcm_alaw":
        command.extend(["-c:a", "aac", "-profile:a", "aac_low", "-ac", "1", "-ar", "8000", "-b:a", "32k"])
    elif (asset.audio_codec or "").lower() == "aac":
        command.extend(["-c:a", "copy"])
    else:
        command.extend(["-c:a", "aac", "-profile:a", "aac_low", "-ac", "1", "-ar", "8000", "-b:a", "32k"])
    # The atomic staging name ends in .part, so tell FFmpeg explicitly that
    # the muxer is MP4 before writing it.
    command.extend(["-movflags", "+faststart", "-f", "mp4", str(part_path)])
    completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=7200)
    if completed.returncode != 0 or not part_path.is_file() or part_path.stat().st_size == 0:
        try:
            part_path.unlink()
        except FileNotFoundError:
            pass
        raise RuntimeError("FFMPEG_NORMALIZATION_FAILED")
    _container, output_audio_codec, output_duration_ms = _validate_output(part_path, expected_duration_ms=expected_duration_ms, source_audio_codec=asset.audio_codec)
    os.replace(part_path, final_path)
    _register_normalized(asset, volume, final_path, output_audio_codec, output_duration_ms)
    return "created"


def main() -> None:
    parser = argparse.ArgumentParser(description="Create playback-normalized copies for a bounded date")
    parser.add_argument("--date", required=True, help="UTC source date, YYYY-MM-DD")
    args = parser.parse_args()
    day_start = datetime.fromisoformat(args.date).replace(tzinfo=timezone.utc)
    if day_start < datetime(2026, 9, 1, tzinfo=timezone.utc):
        parser.error("dates before 2026-09-01 are protected and cannot be normalized")
    day_end = day_start + timedelta(days=1)
    with SessionLocal() as session:
        rows = session.execute(
            select(LocalAsset, SourceRecording, StorageVolume)
            .join(SourceRecording, SourceRecording.id == LocalAsset.source_recording_id)
            .join(StorageVolume, StorageVolume.id == LocalAsset.storage_volume_id)
            .where(
                LocalAsset.asset_role == "source_archive",
                LocalAsset.verification_state == "verified",
                LocalAsset.lifecycle_state == "present",
                SourceRecording.source_start >= day_start,
                SourceRecording.source_start < day_end,
                StorageVolume.id == "archive-a",
            )
            .order_by(SourceRecording.source_start, LocalAsset.id)
        ).all()
    print(f"selected={len(rows)}")
    for asset, source, volume in rows:
        result = _normalize_one(asset, source, volume)
        print(f"asset={asset.id} result={result}", flush=True)


if __name__ == "__main__":
    main()
