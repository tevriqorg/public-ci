from __future__ import annotations

import uuid
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import aliased
from sqlalchemy.orm import Session

from app.models import ArchiveJob, AuditEvent, LocalAsset, SourceRecording, Tombstone, utcnow


ACTIVE_JOB_STATES = {"queued", "resolving_source", "downloading", "verifying", "paused"}
ACTIVE_JOB_PREDICATE = text("state IN ('queued','resolving_source','downloading','verifying','paused')")
LEASE_SECONDS = 15 * 60


@dataclass(frozen=True)
class QueueResult:
    queued: tuple[str, ...]
    already_archived: tuple[str, ...]
    already_queued: tuple[str, ...]
    unavailable: tuple[str, ...]
    not_found: tuple[str, ...]


def enqueue_archive_jobs(
    session: Session,
    source_recording_ids: list[str],
    *,
    requested_by: str = "web-ui",
    allow_expired_revalidation: bool = False,
) -> QueueResult:
    """Create idempotent archive jobs; no source locator is persisted."""
    queued: list[str] = []
    already_archived: list[str] = []
    already_queued: list[str] = []
    unavailable: list[str] = []
    not_found: list[str] = []
    for source_id in dict.fromkeys(source_recording_ids):
        source = session.get(SourceRecording, source_id)
        if source is None:
            not_found.append(source_id)
            continue
        tombstone = session.scalar(
            select(Tombstone.id).where(
                Tombstone.source_recording_id == source.id,
                Tombstone.suppress_redownload.is_(True),
            )
        )
        if tombstone:
            unavailable.append(source_id)
            continue
        if source.source_state != "available":
            if source.source_state != "expired" or not allow_expired_revalidation:
                unavailable.append(source_id)
                continue
            # A manual retry may ask the worker to re-check an expired source,
            # but enqueueing is not evidence that the NVR segment exists again.
            # Keep the persisted source fact expired until the worker resolves
            # the real segment successfully.
            session.add(
                AuditEvent(
                    id=f"audit-{uuid.uuid4().hex}",
                    event_type="source_revalidation_requested",
                    entity_type="source_recording",
                    entity_id=source.id,
                    actor=requested_by[:128],
                    payload_redacted=None,
                )
            )
        if session.scalar(
            select(LocalAsset.id).where(
                LocalAsset.source_recording_id == source.id,
                LocalAsset.asset_role == "source_archive",
                LocalAsset.verification_state == "verified",
                LocalAsset.lifecycle_state == "present",
                LocalAsset.archive_source_snapshot_id.is_not(None),
            )
        ):
            already_archived.append(source_id)
            continue
        if session.scalar(
            select(ArchiveJob.id).where(
                ArchiveJob.source_recording_id == source.id,
                ArchiveJob.state.in_(ACTIVE_JOB_STATES),
            )
        ):
            already_queued.append(source_id)
            continue
        job_id = f"job-{uuid.uuid4().hex}"
        requested_at = utcnow()
        values = {
            "id": job_id,
            "source_recording_id": source.id,
            "requested_by": requested_by[:128],
            "requested_at": requested_at,
            "state": "queued",
            "attempt_count": 0,
            "bytes_received": 0,
        }
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            statement = sqlite_insert(ArchiveJob).values(**values).on_conflict_do_nothing(
                index_elements=[ArchiveJob.source_recording_id],
                index_where=ACTIVE_JOB_PREDICATE,
            )
            inserted = session.execute(statement).rowcount == 1
        elif dialect == "postgresql":
            statement = pg_insert(ArchiveJob).values(**values).on_conflict_do_nothing(
                index_elements=[ArchiveJob.source_recording_id],
                index_where=ACTIVE_JOB_PREDICATE,
            )
            inserted = session.execute(statement).rowcount == 1
        else:
            # The supported runtime is local SQLite. Keep a conservative
            # compatibility path for test/maintenance tools on other engines.
            job = ArchiveJob(**values)
            session.add(job)
            session.flush()
            inserted = True
        if not inserted:
            already_queued.append(source_id)
            continue
        session.add(
            AuditEvent(
                id=f"audit-{job_id}",
                event_type="archive_requested",
                entity_type="archive_job",
                entity_id=job_id,
                actor=requested_by[:128],
                payload_redacted=None,
            )
        )
        queued.append(job_id)
    session.flush()
    return QueueResult(tuple(queued), tuple(already_archived), tuple(already_queued), tuple(unavailable), tuple(not_found))


def claim_next_archive_job(
    session: Session,
    worker_id: str = "archive-worker",
    now: datetime | None = None,
) -> ArchiveJob | None:
    """Atomically claim one queued job before any network I/O."""
    now = now or datetime.now(timezone.utc)
    prior_job = aliased(ArchiveJob)
    prior_source = aliased(SourceRecording)
    camera_last_attempt = (
        select(func.max(prior_job.started_at))
        .join(prior_source, prior_job.source_recording_id == prior_source.id)
        .where(
            prior_source.nvr_id == SourceRecording.nvr_id,
            prior_source.track_id == SourceRecording.track_id,
        )
        .correlate(SourceRecording)
        .scalar_subquery()
    )
    job_id = session.scalar(
        select(ArchiveJob)
        .join(SourceRecording, ArchiveJob.source_recording_id == SourceRecording.id)
        .where(ArchiveJob.state == "queued")
        # First rotate across cameras by last dispatch, then work each camera's
        # oldest eligible source first to reduce overwrite risk.
        .order_by(
            camera_last_attempt.asc(),
            SourceRecording.source_start.asc(),
            SourceRecording.source_end.asc(),
            ArchiveJob.id,
        )
        .limit(1)
    )
    if job_id is None:
        return None
    token = uuid.uuid4().hex
    expires = now + timedelta(seconds=LEASE_SECONDS)
    result = session.execute(
        update(ArchiveJob)
        .where(ArchiveJob.id == job_id.id, ArchiveJob.state == "queued")
        .values(
            state="resolving_source",
            attempt_count=ArchiveJob.attempt_count + 1,
            started_at=now,
            worker_id=worker_id[:128],
            lease_token=token,
            lease_expires_at=expires,
            lease_phase="resolving_source",
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        session.rollback()
        return None
    job = job_id
    job.state = "resolving_source"
    job.attempt_count += 1
    job.started_at = now
    job.worker_id = worker_id[:128]
    job.lease_token = token
    job.lease_expires_at = expires
    job.lease_phase = "resolving_source"
    session.flush()
    return job


def recover_interrupted_archive_jobs(session: Session) -> int:
    """Return jobs interrupted by a worker restart to the durable queue."""
    now = datetime.now(timezone.utc)
    jobs = session.scalars(
        select(ArchiveJob).where(
            ArchiveJob.state.in_({"resolving_source", "downloading", "verifying"}),
            or_(ArchiveJob.lease_expires_at.is_(None), ArchiveJob.lease_expires_at <= now),
        )
    ).all()
    for job in jobs:
        job.state = "queued"
        job.error_code = None
        job.error_detail_redacted = None
        job.temp_path = None
        job.bytes_received = 0
        job.worker_id = None
        job.lease_token = None
        job.lease_expires_at = None
        job.lease_phase = None
    session.flush()
    return len(jobs)


RETRYABLE_ARCHIVE_ERRORS = (
    "NVR_SEARCH_TRANSPORT_FAILED",
    "NVR_SEARCH_HTTP_5",
    "NVR_BANDWIDTH_LIMIT",
    "NVR_DOWNLOAD_FAILED",
    "NVR_DOWNLOAD_HTTP_5",
    "STORAGE_NOT_READY",
    "ORPHAN_FINAL_QUARANTINED",
    "ARCHIVE_COMMIT_OR_FINALIZE_FAILED",
)


def retry_due_archive_jobs(
    session: Session,
    *,
    now: datetime | None = None,
    max_attempts: int = 5,
    base_delay_seconds: int = 300,
    max_delay_seconds: int = 21_600,
) -> int:
    """Requeue only bounded transient failures after exponential backoff."""
    now = now or datetime.now(timezone.utc)
    jobs = session.scalars(
        select(ArchiveJob).where(
            ArchiveJob.state == "failed",
            ArchiveJob.completed_at.is_not(None),
            ArchiveJob.attempt_count < max_attempts,
        )
    ).all()
    retried = 0
    for job in jobs:
        code = job.error_code or ""
        if not any(code.startswith(prefix) for prefix in RETRYABLE_ARCHIVE_ERRORS):
            continue
        attempt = max(1, job.attempt_count)
        delay = min(base_delay_seconds * (2 ** (attempt - 1)), max_delay_seconds)
        completed_at = job.completed_at
        if completed_at is None:
            continue
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=timezone.utc)
        if completed_at + timedelta(seconds=delay) > now:
            continue
        previous_code = job.error_code
        job.state = "queued"
        job.error_code = None
        job.error_detail_redacted = None
        job.completed_at = None
        job.bytes_received = 0
        session.add(
            AuditEvent(
                id=f"audit-retry-{uuid.uuid4().hex}",
                event_type="archive_retry_scheduled",
                entity_type="archive_job",
                entity_id=job.id,
                actor="archive-recovery",
                payload_redacted=json.dumps(
                    {"attempt": attempt + 1, "previous_error_code": previous_code},
                    sort_keys=True,
                ),
            )
        )
        retried += 1
    session.flush()
    return retried


def renew_archive_claim(
    session: Session,
    job_id: str,
    worker_id: str,
    token: str,
    phase: str,
    now: datetime | None = None,
) -> bool:
    now = now or datetime.now(timezone.utc)
    result = session.execute(
        update(ArchiveJob)
        .where(
            ArchiveJob.id == job_id,
            ArchiveJob.worker_id == worker_id,
            ArchiveJob.lease_token == token,
            ArchiveJob.lease_expires_at > now,
        )
        .values(lease_phase=phase, lease_expires_at=now + timedelta(seconds=LEASE_SECONDS))
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return result.rowcount == 1


def release_archive_claim(
    session: Session,
    job_id: str,
    worker_id: str,
    token: str,
    phase: str | None = None,
) -> bool:
    result = session.execute(
        update(ArchiveJob)
        .where(
            ArchiveJob.id == job_id,
            ArchiveJob.worker_id == worker_id,
            ArchiveJob.lease_token == token,
        )
        .values(worker_id=None, lease_token=None, lease_expires_at=None, lease_phase=phase)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return result.rowcount == 1


def _record_job_control_audit(session: Session, job: ArchiveJob, event_type: str, actor: str) -> None:
    session.add(
        AuditEvent(
            id=f"audit-{uuid.uuid4().hex}",
            event_type=event_type,
            entity_type="archive_job",
            entity_id=job.id,
            actor=actor[:128],
            payload_redacted=None,
        )
    )


def set_queued_jobs_state(
    session: Session,
    *,
    from_states: set[str],
    to_state: str,
    event_type: str,
    actor: str = "web-ui",
) -> tuple[int, int]:
    """Apply a safe control action to jobs not currently doing network I/O.

    The single archive worker owns resolving/downloading/verifying jobs. Those
    states are deliberately not mutated by this helper, so a toolbar action
    cannot turn an in-flight download into a false pause/cancel.
    """
    jobs = session.scalars(select(ArchiveJob).where(ArchiveJob.state.in_(from_states))).all()
    for job in jobs:
        job.state = to_state
        _record_job_control_audit(session, job, event_type, actor)
    session.flush()
    active_count = session.query(ArchiveJob).where(
        ArchiveJob.state.in_({"resolving_source", "downloading", "verifying"})
    ).count()
    return len(jobs), active_count


def retry_failed_archive_jobs(
    session: Session,
    *,
    actor: str = "web-ui",
    month_prefix: str | None = None,
) -> QueueResult:
    """Create fresh idempotent jobs for retryable failures, retaining history."""
    failed_jobs = session.scalars(select(ArchiveJob).where(ArchiveJob.state == "failed")).all()
    retryable_sources: list[str] = []
    expired_sources: list[str] = []
    for job in failed_jobs:
        source = session.get(SourceRecording, job.source_recording_id)
        if month_prefix and (source is None or source.source_start.strftime("%Y-%m") != month_prefix):
            continue
        if job.error_code != "NVR_SOURCE_SEGMENT_NOT_FOUND":
            retryable_sources.append(job.source_recording_id)
            continue
        if source is not None and source.source_state == "available":
            source.source_state = "expired"
            source.expired_observed_at = utcnow()
            session.add(
                AuditEvent(
                    id=f"audit-{uuid.uuid4().hex}",
                    event_type="source_expired_observed",
                    entity_type="source_recording",
                    entity_id=source.id,
                    actor=actor[:128],
                    payload_redacted=None,
                )
            )
        expired_sources.append(job.source_recording_id)
    result = enqueue_archive_jobs(session, list(dict.fromkeys(retryable_sources)), requested_by=f"{actor}-retry")
    return QueueResult(
        result.queued,
        result.already_archived,
        result.already_queued,
        tuple(dict.fromkeys((*result.unavailable, *expired_sources))),
        result.not_found,
    )
