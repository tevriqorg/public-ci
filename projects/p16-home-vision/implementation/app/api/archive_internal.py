from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.domain.archive_queue import (
    enqueue_archive_jobs,
    retry_failed_archive_jobs,
    set_queued_jobs_state,
)
from app.models import ArchiveJob, LocalAsset, SourceRecording


class ArchiveBatchRequest(BaseModel):
    source_recording_ids: list[str] = Field(min_length=1, max_length=1000)


router = APIRouter(prefix="/internal/archive", tags=["archive-internal"])


def _worker_heartbeat() -> dict[str, Any]:
    path = get_settings().archive_heartbeat_path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        heartbeat_at = datetime.fromisoformat(str(payload["heartbeat_at"]).replace("Z", "+00:00"))
        age_seconds = max(0.0, (datetime.now(timezone.utc) - heartbeat_at).total_seconds())
        return {
            "status": "healthy" if age_seconds <= 30 else "stale",
            "phase": payload.get("phase"),
            "heartbeat_at": heartbeat_at,
            "age_seconds": round(age_seconds, 1),
        }
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return {"status": "not_seen", "phase": None, "heartbeat_at": None, "age_seconds": None}


def _worker_state() -> tuple[bool, dict[str, Any], bool]:
    configured = get_settings().archive_queue_enabled
    heartbeat = _worker_heartbeat() if configured else {"status": "not_configured"}
    ready = configured and heartbeat.get("status") == "healthy"
    return configured, heartbeat, ready


def _require_queue_configured() -> None:
    configured, _heartbeat, _ready = _worker_state()
    if not configured:
        raise HTTPException(status_code=503, detail="ARCHIVE_WORKER_NOT_CONFIGURED")


def _require_enqueue_ready() -> None:
    configured, heartbeat, ready = _worker_state()
    if not configured:
        raise HTTPException(status_code=503, detail="ARCHIVE_WORKER_NOT_CONFIGURED")
    if not ready:
        raise HTTPException(
            status_code=503,
            detail=f"ARCHIVE_WORKER_{str(heartbeat.get('status') or 'UNAVAILABLE').upper()}",
        )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _current_job_rate(job: ArchiveJob, now: datetime) -> float:
    if job.state != "downloading" or not job.started_at or not job.bytes_received:
        return 0.0
    started_at = _as_utc(job.started_at)
    if started_at is None:
        return 0.0
    elapsed = max(0.0, (now - started_at).total_seconds())
    return round(job.bytes_received / elapsed, 1) if elapsed > 0 else 0.0


@router.get("/status")
def archive_status() -> dict[str, Any]:
    settings = get_settings()
    configured, heartbeat, ready = _worker_state()
    control_actions = ["pause_queued", "cancel_waiting"] if configured else []
    if ready:
        control_actions.extend(["resume_paused", "retry_failed"])
    return {
        "configured": configured,
        # Kept for backward compatibility with the first runtime projection.
        # New clients must use enqueue_ready before creating/restarting work.
        "enqueue_enabled": configured,
        "enqueue_ready": ready,
        "worker": "archive_worker" if configured else "not_configured",
        "download_method": settings.archive_download_method if configured else None,
        "heartbeat": heartbeat,
        "control_actions": control_actions,
    }


@router.get("/metrics")
def archive_metrics(
    month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Return a reproducible, read-only download accounting snapshot.

    The API intentionally labels the rate projection as instantaneous: the
    current schema has no historical byte samples, so it must not be presented
    as a measured end-to-end average.
    """
    now = datetime.now(timezone.utc)
    recordings = {row.id: row for row in session.scalars(select(SourceRecording)).all()}
    jobs = session.scalars(select(ArchiveJob).order_by(ArchiveJob.requested_at, ArchiveJob.id)).all()
    scoped_jobs = [
        job for job in jobs
        if month is None
        or (
            recordings.get(job.source_recording_id) is not None
            and recordings[job.source_recording_id].source_start.strftime("%Y-%m") == month
        )
    ]
    source_ids = {job.source_recording_id for job in scoped_jobs}
    assets = session.scalars(
        select(LocalAsset).where(LocalAsset.source_recording_id.in_(source_ids))
    ).all() if source_ids else []
    verified_present_bytes = sum(
        asset.size_bytes or 0
        for asset in assets
        if asset.verification_state == "verified" and asset.lifecycle_state == "present"
    )
    failed_jobs = [job for job in scoped_jobs if job.state == "failed"]
    non_failed_jobs = [job for job in scoped_jobs if job.state != "failed"]
    active_jobs = [
        job for job in non_failed_jobs
        if job.state in {"resolving_source", "downloading", "verifying"}
    ]
    unfinished_jobs = [job for job in non_failed_jobs if job.state in {"queued", "paused"}]
    first_requested_at = min((job.requested_at for job in scoped_jobs), default=None)
    first_started_at = min(
        (job.started_at for job in scoped_jobs if job.started_at),
        default=None,
    )
    elapsed = max(
        0.0,
        (now - _as_utc(first_requested_at)).total_seconds(),
    ) if first_requested_at else 0.0
    current_rate = round(sum(_current_job_rate(job, now) for job in active_jobs), 1)
    expected_bytes = sum(
        recordings[job.source_recording_id].source_size_if_known or 0
        for job in scoped_jobs
        if job.source_recording_id in recordings
    )
    expected_non_failed_bytes = sum(
        recordings[job.source_recording_id].source_size_if_known or 0
        for job in non_failed_jobs
        if job.source_recording_id in recordings
    )
    queued_source_bytes = sum(
        recordings[job.source_recording_id].source_size_if_known or 0
        for job in unfinished_jobs
        if job.source_recording_id in recordings
    )
    active_remaining_bytes = sum(
        max(
            0,
            (recordings[job.source_recording_id].source_size_if_known or 0)
            - (job.bytes_received or 0),
        )
        for job in active_jobs
        if job.source_recording_id in recordings
    )
    state_counts: dict[str, int] = {}
    for job in scoped_jobs:
        state_counts[job.state] = state_counts.get(job.state, 0) + 1

    return {
        "snapshot_at": now,
        "month": month,
        "job_count": len(scoped_jobs),
        "state_counts": state_counts,
        "first_requested_at": _as_utc(first_requested_at),
        "first_started_at": _as_utc(first_started_at),
        "elapsed_seconds_since_first_request": round(elapsed, 3),
        "current_throughput_bytes_per_second": current_rate,
        "throughput_basis": "active_job_bytes_received_divided_by_active_job_elapsed",
        "historical_throughput_available": False,
        "projected_bytes_at_current_rate": round(current_rate * elapsed),
        "source_size_bytes": expected_bytes,
        "source_size_non_failed_bytes": expected_non_failed_bytes,
        "bytes_received_bytes": sum(job.bytes_received or 0 for job in scoped_jobs),
        "bytes_received_non_failed_bytes": sum(job.bytes_received or 0 for job in non_failed_jobs),
        "verified_present_bytes": verified_present_bytes,
        "failed_source_size_bytes": sum(
            recordings[job.source_recording_id].source_size_if_known or 0
            for job in failed_jobs
            if job.source_recording_id in recordings
        ),
        "failed_bytes_received_bytes": sum(job.bytes_received or 0 for job in failed_jobs),
        "queued_source_size_bytes": queued_source_bytes,
        "active_remaining_bytes": active_remaining_bytes,
        "remaining_to_receive_bytes": queued_source_bytes + active_remaining_bytes,
        "caveats": [
            "当前速率是活动任务的瞬时/任务内平均值，不是整个时间窗的历史平均值。",
            "当前版本没有持久化吞吐采样，因此差额不能唯一归因于网络、源端、暂停或校验。",
        ],
    }


@router.post("/batch")
def queue_archive_batch(
    request: ArchiveBatchRequest,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    _require_enqueue_ready()
    result = enqueue_archive_jobs(
        session,
        request.source_recording_ids,
        allow_expired_revalidation=True,
    )
    session.commit()
    return {
        "queued_job_ids": list(result.queued),
        "queued_count": len(result.queued),
        "already_archived_source_ids": list(result.already_archived),
        "already_queued_source_ids": list(result.already_queued),
        "unavailable_source_ids": list(result.unavailable),
        "not_found_source_ids": list(result.not_found),
    }


@router.post("/jobs/pause-all")
def pause_all_archive_jobs(session: Session = Depends(get_session)) -> dict[str, Any]:
    _require_queue_configured()
    changed, active = set_queued_jobs_state(
        session,
        from_states={"queued"},
        to_state="paused",
        event_type="archive_paused",
    )
    session.commit()
    return {"action": "pause_all", "changed_count": changed, "active_unaffected_count": active}


@router.post("/jobs/resume-all")
def resume_all_archive_jobs(session: Session = Depends(get_session)) -> dict[str, Any]:
    _require_enqueue_ready()
    changed, active = set_queued_jobs_state(
        session,
        from_states={"paused"},
        to_state="queued",
        event_type="archive_resumed",
    )
    session.commit()
    return {"action": "resume_all", "changed_count": changed, "active_unaffected_count": active}


@router.post("/jobs/cancel-unfinished")
def cancel_unfinished_archive_jobs(session: Session = Depends(get_session)) -> dict[str, Any]:
    _require_queue_configured()
    changed, active = set_queued_jobs_state(
        session,
        from_states={"queued", "paused"},
        to_state="cancelled",
        event_type="archive_cancelled",
    )
    session.commit()
    return {"action": "cancel_unfinished", "changed_count": changed, "active_unaffected_count": active}


@router.post("/jobs/retry-failed")
def retry_failed_archive_jobs_endpoint(
    month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    _require_enqueue_ready()
    result = retry_failed_archive_jobs(session, month_prefix=month)
    session.commit()
    return {
        "action": "retry_failed",
        "queued_job_ids": list(result.queued),
        "queued_count": len(result.queued),
        "already_archived_source_ids": list(result.already_archived),
        "already_queued_source_ids": list(result.already_queued),
        "unavailable_source_ids": list(result.unavailable),
        "not_found_source_ids": list(result.not_found),
    }
