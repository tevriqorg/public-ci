from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
import fcntl
import logging
import os
from pathlib import Path
import time as time_module
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import exists, select

from app.config import get_settings
from app.db import SessionLocal
from app.domain.media_access import resolve_media_path
from app.domain.thumbnails import (
    THUMBNAIL_STREAM_MAX_BYTES,
    ThumbnailError,
    generate_thumbnail,
    generate_thumbnail_from_stream,
    source_thumbnail_path,
    thumbnail_is_usable,
    thumbnail_path,
)
from app.infra.nvr.hikvision import HikvisionIsapiClient, NvrProtocolError
from scripts.archive_worker import _find_exact_segment
from app.models import LocalAsset, ScanRun, SourceRecording, StorageVolume


LOGGER = logging.getLogger("home_vision.thumbnail_worker")
LOCK_PATH = Path("/state/thumbnail-cache/.worker.lock")


def _as_utc(value: datetime) -> datetime:
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).astimezone(timezone.utc)


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError("THUMBNAIL_TIMEZONE_INVALID") from exc


def _select_sources(session, *, now: datetime, zone: ZoneInfo) -> list[SourceRecording]:
    settings = get_settings()
    successful_scan = exists(
        select(ScanRun.id).where(
            ScanRun.id == SourceRecording.last_scan_id,
            ScanRun.state == "success",
            ScanRun.query_start <= SourceRecording.source_start,
            ScanRun.query_end > SourceRecording.source_start,
        )
    )
    filters = [
        SourceRecording.source_state != "unknown",
        SourceRecording.last_confirmed_at.is_not(None),
        SourceRecording.last_scan_id.is_not(None),
        successful_scan,
    ]
    if settings.nvr_scan_min_date is not None:
        floor = datetime.combine(settings.nvr_scan_min_date, datetime.min.time(), tzinfo=timezone.utc)
        filters.append(SourceRecording.source_start >= floor)
    rows = session.scalars(
        select(SourceRecording)
        .where(*filters)
        .order_by(SourceRecording.source_start, SourceRecording.id)
    ).all()
    return list(rows)


def _target_for_source(session, source: SourceRecording, cache_root: Path) -> tuple[Path, LocalAsset | None]:
    local_asset = session.scalar(
        select(LocalAsset)
        .where(
            LocalAsset.source_recording_id == source.id,
            LocalAsset.asset_role == "source_archive",
            LocalAsset.verification_state == "verified",
            LocalAsset.lifecycle_state == "present",
        )
        .order_by(LocalAsset.verified_at.desc(), LocalAsset.id.desc())
    )
    if local_asset is not None:
        volume = session.get(StorageVolume, local_asset.storage_volume_id)
        if volume is not None:
            return thumbnail_path(Path(volume.mount_path_runtime), local_asset.id), local_asset
    return source_thumbnail_path(cache_root, source.id, source.locator_fingerprint), None


def _normalized_path(session, source: SourceRecording) -> Path | None:
    row = session.execute(
        select(LocalAsset, StorageVolume)
        .join(StorageVolume, StorageVolume.id == LocalAsset.storage_volume_id)
        .where(
            LocalAsset.source_recording_id == source.id,
            LocalAsset.asset_role == "normalized_playback",
            LocalAsset.verification_state == "verified",
            LocalAsset.lifecycle_state == "present",
            StorageVolume.online.is_(True),
        )
        .order_by(LocalAsset.verified_at.desc(), LocalAsset.id.desc())
    ).first()
    if row is None:
        return None
    asset, volume = row
    path = resolve_media_path(Path(volume.mount_path_runtime), asset.relative_path)
    return path if path.is_file() else None


def _generate_from_nvr(client: HikvisionIsapiClient, source: SourceRecording, output_path: Path) -> str:
    segment = _find_exact_segment(client, source)
    seek_seconds = min(10.0, max(0.5, source.duration_ms / 2000))
    retry_seek = min(30.0, max(1.0, source.duration_ms / 2000))
    seeks = (seek_seconds,) if retry_seek == seek_seconds else (seek_seconds, retry_seek)
    for seek in seeks:
        try:
            with client.stream_download(segment.playback_uri, max_bytes=THUMBNAIL_STREAM_MAX_BYTES) as chunks:
                return generate_thumbnail_from_stream(chunks, output_path, seek_seconds=seek, overwrite=True)
        except ThumbnailError:
            if seek == seeks[-1]:
                raise
    raise ThumbnailError("THUMBNAIL_STREAM_FFMPEG_FAILED")


def run_once(*, now: datetime | None = None) -> dict[str, int]:
    settings = get_settings()
    zone = _timezone(settings.thumbnail_timezone)
    current = now or datetime.now(timezone.utc)
    cache_root = settings.data_root.parent
    nvr_client = None
    if settings.nvr_base_url and settings.nvr_username and settings.nvr_password:
        nvr_client = HikvisionIsapiClient(
            base_url=settings.nvr_base_url,
            username=settings.nvr_username,
            password=settings.nvr_password.get_secret_value(),
            download_method=settings.archive_download_method,
        )
    try:
        with SessionLocal() as session:
            rows = _select_sources(session, now=current, zone=zone)
            counts = {"selected": len(rows), "created": 0, "already_present": 0, "deferred": 0, "failed": 0}
            print(f"selected={len(rows)}", flush=True)
            for source in rows:
                output_path, _local_asset = _target_for_source(session, source, cache_root)
                try:
                    if thumbnail_is_usable(output_path):
                        result = "already_present"
                    else:
                        normalized = _normalized_path(session, source)
                        if normalized is not None:
                            try:
                                result = generate_thumbnail(normalized, output_path, overwrite=True, reject_uniform=True)
                            except ThumbnailError:
                                try:
                                    result = generate_thumbnail(normalized, output_path, overwrite=True)
                                except ThumbnailError:
                                    if nvr_client is None:
                                        raise
                                    result = _generate_from_nvr(nvr_client, source, output_path)
                        elif nvr_client is not None:
                            result = _generate_from_nvr(nvr_client, source, output_path)
                        else:
                            counts["deferred"] += 1
                            print(f"source={source.id} result=deferred normalized_not_ready", flush=True)
                            continue
                    counts[result] += 1
                    print(f"source={source.id} result={result}", flush=True)
                except (NvrProtocolError, ThumbnailError, OSError, ValueError) as exc:
                    counts["failed"] += 1
                    print(f"source={source.id} result=failed error={getattr(exc, 'code', type(exc).__name__)}", flush=True)
            print(" ".join(f"{key}={value}" for key, value in counts.items()), flush=True)
            return counts
    finally:
        if nvr_client is not None:
            nvr_client.close()


@contextmanager
def _single_instance():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("thumbnail worker already running")
        yield


def _next_noon(now: datetime, zone: ZoneInfo) -> datetime:
    settings = get_settings()
    local_now = now.astimezone(zone)
    candidate = datetime.combine(
        local_now.date(),
        time(settings.thumbnail_schedule_hour, settings.thumbnail_schedule_minute),
        tzinfo=zone,
    )
    return candidate if candidate > local_now else candidate + timedelta(days=1)


def run_watch() -> None:
    settings = get_settings()
    zone = _timezone(settings.thumbnail_timezone)
    with _single_instance():
        run_once()
        while True:
            time_module.sleep(max(1, settings.thumbnail_poll_seconds))
            run_once()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create cached thumbnails from normalized NVR recordings")
    parser.add_argument("--once", action="store_true", help="run the catch-up pass once")
    parser.add_argument("--watch", action="store_true", help="scan for new normalized recordings every poll interval")
    args = parser.parse_args()
    if args.once == args.watch:
        parser.error("必须指定 --once 或 --watch")
    logging.basicConfig(level=os.environ.get("HOME_VISION_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    if args.watch:
        run_watch()
    else:
        with _single_instance():
            raise SystemExit(1 if run_once()["failed"] else 0)


if __name__ == "__main__":
    main()
