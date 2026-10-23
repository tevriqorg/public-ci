from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import logging
import os
import time as time_module
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.domain.nvr_scan import scan_camera_range
from app.domain.auto_archive import enqueue_closed_day_backlog
from app.infra.nvr.hikvision import HikvisionIsapiClient
from app.models import ArchiveJob, CameraTrack, LocalAsset, NvrDevice, ScanRun, SourceRecording, Tombstone


LOGGER = logging.getLogger("home_vision.nvr_scan")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须是 YYYY-MM-DD") from exc


def _date_range(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError("NVR_SCAN_RANGE_INVALID")
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _has_successful_full_day_scan(
    session,
    *,
    nvr_id: str,
    track_id: str,
    day_start: datetime,
    day_end: datetime,
) -> bool:
    return bool(
        session.scalar(
            select(ScanRun.id)
            .where(
                ScanRun.nvr_id == nvr_id,
                ScanRun.track_id == track_id,
                ScanRun.state == "success",
                ScanRun.query_start <= day_start,
                ScanRun.query_end >= day_end,
            )
            .limit(1)
        )
    )


def _day_has_unsettled_archive_sources(
    session,
    *,
    nvr_id: str,
    track_id: str,
    day_start: datetime,
    day_end: datetime,
) -> bool:
    sources = session.scalars(
        select(SourceRecording).where(
            SourceRecording.nvr_id == nvr_id,
            SourceRecording.track_id == track_id,
            SourceRecording.source_state == "available",
            SourceRecording.source_start < day_end,
            SourceRecording.source_end >= day_start,
        )
    ).all()
    active_states = {"queued", "resolving_source", "downloading", "verifying", "paused"}
    for source in sources:
        if session.scalar(
            select(Tombstone.id).where(
                Tombstone.source_recording_id == source.id,
                Tombstone.suppress_redownload.is_(True),
            )
        ):
            continue
        if session.scalar(
            select(LocalAsset.id).where(
                LocalAsset.source_recording_id == source.id,
                LocalAsset.asset_role == "source_archive",
                LocalAsset.verification_state == "verified",
                LocalAsset.lifecycle_state == "present",
                LocalAsset.archive_source_snapshot_id.is_not(None),
            )
        ):
            continue
        if session.scalar(
            select(ArchiveJob.id).where(
                ArchiveJob.source_recording_id == source.id,
                ArchiveJob.state.in_(active_states),
            )
        ):
            continue
        return True
    return False


def scan_once(*, start_day: date, end_day: date, retry_missing_only: bool = False) -> int:
    settings = get_settings()
    if not settings.nvr_base_url or not settings.nvr_username or not settings.nvr_password:
        raise RuntimeError("NVR_SCAN_CONFIG_MISSING")
    if settings.nvr_scan_min_date is not None:
        if end_day < settings.nvr_scan_min_date:
            return 0
        start_day = max(start_day, settings.nvr_scan_min_date)

    requested_tracks = {value.strip() for value in (settings.nvr_tracks or "").split(",") if value.strip()}
    with SessionLocal() as session:
        nvr = session.scalar(
            select(NvrDevice).where(NvrDevice.id == settings.nvr_id, NvrDevice.enabled.is_(True))
            if settings.nvr_id
            else select(NvrDevice).where(NvrDevice.enabled.is_(True)).order_by(NvrDevice.id)
        )
        if nvr is None:
            raise RuntimeError("NVR_SCAN_DEVICE_MISSING")
        cameras = list(session.scalars(select(CameraTrack).where(CameraTrack.nvr_id == nvr.id, CameraTrack.enabled.is_(True)).order_by(CameraTrack.track_id)).all())
        if requested_tracks:
            cameras = [camera for camera in cameras if camera.track_id in requested_tracks]
        if not cameras:
            raise RuntimeError("NVR_SCAN_CAMERA_MISSING")

        try:
            business_zone = ZoneInfo(nvr.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise RuntimeError("NVR_TIMEZONE_INVALID") from exc

        discovered = 0
        with HikvisionIsapiClient(
            base_url=settings.nvr_base_url,
            username=settings.nvr_username,
            password=settings.nvr_password.get_secret_value(),
        ) as client:
            for current_day in _date_range(start_day, end_day):
                query_start = datetime.combine(current_day, time.min, tzinfo=business_zone).astimezone(timezone.utc)
                query_end = datetime.combine(current_day + timedelta(days=1), time.min, tzinfo=business_zone).astimezone(timezone.utc)
                for camera in cameras:
                    is_recent = current_day >= datetime.now(business_zone).date() - timedelta(days=1)
                    if retry_missing_only and not is_recent:
                        already_covered = _has_successful_full_day_scan(
                            session,
                            nvr_id=nvr.id,
                            track_id=camera.track_id,
                            day_start=query_start,
                            day_end=query_end,
                        )
                        unsettled = _day_has_unsettled_archive_sources(
                            session,
                            nvr_id=nvr.id,
                            track_id=camera.track_id,
                            day_start=query_start,
                            day_end=query_end,
                        )
                        if already_covered and not unsettled:
                            continue
                    result = scan_camera_range(
                        session,
                        nvr=nvr,
                        camera=camera,
                        query_start=query_start,
                        query_end=query_end,
                        searcher=client,
                    )
                    session.commit()
                    discovered += result.discovered_count
                    if result.error_code:
                        LOGGER.warning("NVR scan failed: state=%s error=%s track=%s", result.state, result.error_code, camera.track_id)
                    else:
                        LOGGER.info("NVR scan complete: day=%s track=%s segments=%s", current_day, camera.track_id, result.discovered_count)
        # Eligibility is independently fenced by successful per-source scans.
        # A failed peer camera must not suppress preservation for healthy peers.
        auto = enqueue_closed_day_backlog(
            session,
            nvr_id=nvr.id,
            enabled=settings.archive_auto_enqueue,
            timezone_verified=settings.nvr_timezone_verified,
        )
        if auto.enabled and auto.queue_result is not None:
            session.commit()
            LOGGER.info(
                "closed-day auto-archive reconciliation: eligible=%s queued=%s already_queued=%s archived=%s",
                auto.eligible_count,
                len(auto.queue_result.queued),
                len(auto.queue_result.already_queued),
                len(auto.queue_result.already_archived),
            )
        elif settings.archive_auto_enqueue:
            LOGGER.error("closed-day auto-archive disabled: reason=%s", auto.reason)
        return discovered


def _watch_range(
    *,
    today: date,
    start_day: date | None,
    end_day: date | None,
    backfill_days: int = 60,
    scan_min_date: date | None = None,
) -> tuple[date, date]:
    # Explicit ranges remain explicit. The watcher revisits a bounded history
    # window to recover failed/missing scan coverage; successful old full-day
    # scans are skipped before network I/O. The window is an operational query
    # bound, not an NVR retention guarantee.
    if start_day is not None or end_day is not None:
        return start_day or (today - timedelta(days=30)), end_day or today
    start = today - timedelta(days=max(1, backfill_days))
    if scan_min_date is not None:
        start = max(start, scan_min_date)
    return start, today


def _watch_today(settings) -> date:
    """Use the registered NVR's business timezone for closed-day boundaries."""
    with SessionLocal() as session:
        query = select(NvrDevice).where(NvrDevice.enabled.is_(True))
        nvr = session.scalar(query.where(NvrDevice.id == settings.nvr_id) if settings.nvr_id else query.order_by(NvrDevice.id))
        if nvr is None:
            raise RuntimeError("NVR_SCAN_DEVICE_MISSING")
        try:
            return datetime.now(ZoneInfo(nvr.timezone)).date()
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise RuntimeError("NVR_TIMEZONE_INVALID") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded read-only Hikvision history scan")
    parser.add_argument("--start-date", type=_parse_date)
    parser.add_argument("--end-date", type=_parse_date)
    parser.add_argument("--watch", action="store_true", help="repeat a rolling NVR scan")
    parser.add_argument("--interval-seconds", type=int, default=900)
    args = parser.parse_args()
    if args.interval_seconds < 60:
        parser.error("--interval-seconds 不能小于 60")

    logging.basicConfig(level=os.environ.get("HOME_VISION_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    while True:
        today = _watch_today(settings)
        start_day, end_day = _watch_range(
            today=today,
            start_day=args.start_date,
            end_day=args.end_date,
            backfill_days=settings.nvr_scan_initial_days,
            scan_min_date=settings.nvr_scan_min_date,
        )
        scan_once(
            start_day=start_day,
            end_day=end_day,
            retry_missing_only=args.watch,
        )
        if not args.watch:
            return 0
        time_module.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
