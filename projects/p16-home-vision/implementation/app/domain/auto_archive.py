from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.archive_queue import QueueResult, enqueue_archive_jobs
from app.models import NvrDevice, ScanRun, SourceRecording


@dataclass(frozen=True)
class AutoArchiveResult:
    enabled: bool
    cutoff_utc: datetime | None
    eligible_count: int
    queue_result: QueueResult | None
    reason: str | None = None


def closed_day_cutoff_utc(*, now: datetime, timezone_name: str) -> datetime:
    """Return configured local midnight as UTC; invalid zones fail closed."""
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("NVR_TIMEZONE_INVALID") from exc
    instant = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    local_today = instant.astimezone(zone).date()
    return datetime.combine(local_today, time.min, tzinfo=zone).astimezone(timezone.utc)


def enqueue_closed_day_backlog(
    session: Session,
    *,
    nvr_id: str,
    now: datetime | None = None,
    enabled: bool,
    timezone_verified: bool,
) -> AutoArchiveResult:
    """Queue all known, eligible prior-day sources without touching the NVR.

    This only creates P16 local archive jobs. It does not download media. The
    archive worker remains separately gated by its existing runtime controls.
    """
    if not enabled:
        return AutoArchiveResult(False, None, 0, None, "AUTO_ARCHIVE_DISABLED")
    if not timezone_verified:
        return AutoArchiveResult(False, None, 0, None, "NVR_TIMEZONE_NOT_VERIFIED")
    nvr = session.get(NvrDevice, nvr_id)
    if nvr is None or not nvr.enabled:
        return AutoArchiveResult(False, None, 0, None, "NVR_NOT_ENABLED")
    now = now or datetime.now(timezone.utc)
    cutoff = closed_day_cutoff_utc(now=now, timezone_name=nvr.timezone)
    eligible_sources = list(
        session.scalars(
            select(SourceRecording)
            .where(
                SourceRecording.nvr_id == nvr_id,
                SourceRecording.source_state == "available",
                SourceRecording.source_end <= cutoff,
            )
            .order_by(SourceRecording.source_start.asc(), SourceRecording.track_id.asc())
        ).all()
    )
    # Require two successful, post-midnight scans with identical full-day
    # response hashes. This proves the NVR's previously discovered interval
    # stopped growing before the first unattended enqueue.
    source_ids: list[str] = []
    for source in eligible_sources:
        scans = session.scalars(
            select(ScanRun)
            .where(
                ScanRun.nvr_id == nvr_id,
                ScanRun.track_id == source.track_id,
                ScanRun.state == "success",
                ScanRun.query_start <= source.source_start,
                ScanRun.query_end >= source.source_start,
                ScanRun.completed_at >= cutoff,
                ScanRun.response_hash.is_not(None),
            )
            .order_by(ScanRun.completed_at.desc())
            .limit(2)
        ).all()
        # The newest successful matching scan must have actually rediscovered
        # this source. Equal empty-result hashes must never enqueue stale ledger
        # rows that disappeared from the NVR.
        if (
            len(scans) == 2
            and scans[0].response_hash == scans[1].response_hash
            and source.last_scan_id == scans[0].id
        ):
            source_ids.append(source.id)
    result = enqueue_archive_jobs(
        session,
        source_ids,
        requested_by="auto-archive-closed-day",
    )
    return AutoArchiveResult(True, cutoff, len(source_ids), result)
