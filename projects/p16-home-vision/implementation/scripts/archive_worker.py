from __future__ import annotations

import argparse
import json
import os
import re
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.db import SessionLocal
from app.domain.archive_pipeline import (
    ArchiveValidationError,
    VerificationPolicy,
    finalize_archive_job,
    quarantine_partial_file,
)
from app.domain.archive_queue import (
    claim_next_archive_job,
    recover_interrupted_archive_jobs,
    release_archive_claim,
    renew_archive_claim,
    retry_due_archive_jobs,
)
from app.domain.storage import StorageError, select_storage_volume
from app.infra.media.ffprobe import FfprobeMediaProbe
from app.infra.nvr.hikvision import HikvisionIsapiClient, NvrProtocolError, build_search_request_xml
from app.models import AuditEvent, CameraTrack, LocalAsset, NvrDevice, SourceRecording, StorageVolume, Tombstone


HEARTBEAT_PATH = get_settings().archive_heartbeat_path
_last_heartbeat = 0.0


def _touch_heartbeat(phase: str, *, force: bool = False) -> None:
    global _last_heartbeat
    now = time.monotonic()
    if not force and now - _last_heartbeat < 10:
        return
    payload = json.dumps(
        {"heartbeat_at": datetime.now(timezone.utc).isoformat(), "phase": phase},
        ensure_ascii=False,
    )
    temporary = HEARTBEAT_PATH.with_name(f".{HEARTBEAT_PATH.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, HEARTBEAT_PATH)
        _last_heartbeat = now
    except OSError:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _safe_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:80] or "unknown"


def _as_utc(value: datetime) -> datetime:
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).astimezone(timezone.utc)


def _find_exact_segment(
    client: HikvisionIsapiClient,
    source: SourceRecording,
    *,
    search_horizon: timedelta | None = None,
):
    # A small margin tolerates NVR search boundary rounding while the final
    # match remains exact enough to prevent downloading a neighboring segment.
    source_start = _as_utc(source.source_start)
    source_end = _as_utc(source.source_end)
    start = source_start - timedelta(seconds=2)
    end = source_end + timedelta(seconds=2)
    if search_horizon is not None:
        end = max(end, source_start + search_horizon)
    segments = client.search_history(build_search_request_xml(track_id=source.track_id, query_start=start, query_end=end))
    matches = [
        segment for segment in segments
        if segment.track_id == source.track_id
        and abs((segment.source_start - source_start).total_seconds()) <= 1
        and abs((segment.source_end - source_end).total_seconds()) <= 1
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise NvrProtocolError("NVR_SOURCE_SEGMENT_NOT_UNIQUE")

    # Hikvision may keep a rolling result open: repeated searches can return
    # the same start time with a later end time.  Treat the newest covering
    # interval as the same source segment, rather than expiring it merely
    # because the old end timestamp is no longer returned verbatim.
    covering = [
        segment for segment in segments
        if segment.track_id == source.track_id
        and abs((segment.source_start - source_start).total_seconds()) <= 1
        and segment.source_end >= source_end - timedelta(seconds=1)
    ]
    if covering:
        return max(covering, key=lambda segment: segment.source_end)
    raise NvrProtocolError("NVR_SOURCE_SEGMENT_NOT_FOUND")


def _media_matches_segment(segment, *, media, received: int) -> bool:
    policy = VerificationPolicy()
    duration_matches = media.duration_ms > 0 and abs(media.duration_ms - segment.duration_ms) <= policy.duration_tolerance_ms
    size_matches = segment.source_size_if_known is None or abs(received - segment.source_size_if_known) <= policy.size_tolerance_bytes
    return bool(media.has_video and duration_matches and size_matches)


def _adopt_resolved_segment(source: SourceRecording, segment) -> None:
    """Refresh a rolling NVR segment before recording its local asset."""
    source.source_end = segment.source_end
    source.duration_ms = segment.duration_ms
    source.locator_fingerprint = segment.locator_fingerprint
    source.source_size_if_known = segment.source_size_if_known
    source.content_type = segment.content_type
    source.recording_type = segment.recording_type
    source.source_state = "available"
    source.expired_observed_at = None
    source.last_confirmed_at = datetime.now(timezone.utc)


def _is_sqlite_lock(exc: OperationalError) -> bool:
    return "database is locked" in str(exc).lower()


def _recover_interrupted_jobs(session) -> None:
    """Keep a transient SQLite writer conflict from stopping the worker."""
    try:
        recover_interrupted_archive_jobs(session)
        retry_due_archive_jobs(session)
        session.commit()
    except OperationalError as exc:
        if not _is_sqlite_lock(exc):
            raise
        session.rollback()


def _record_archive_failure(session, job, *, error_code: str, detail: str | None = None) -> None:
    now = datetime.now(timezone.utc)
    job.completed_at = now
    job.error_code = error_code[:128]
    job.error_detail_redacted = detail[:512] if detail else None
    session.add(
        AuditEvent(
            id=f"audit-attempt-failed-{job.id}-{job.attempt_count}",
            event_type="archive_attempt_failed",
            entity_type="archive_job",
            entity_id=job.id,
            actor="archive-worker",
            payload_redacted=json.dumps(
                {"attempt": job.attempt_count, "error_code": job.error_code},
                sort_keys=True,
            ),
        )
    )
    if error_code == "ORPHAN_FINAL_QUARANTINED":
        session.add(
            AuditEvent(
                id=f"audit-orphan-quarantined-{job.id}-{job.attempt_count}",
                event_type="archive_orphan_quarantined",
                entity_type="archive_job",
                entity_id=job.id,
                actor="archive-worker",
                payload_redacted=None,
            )
        )


def process_one(
    session,
    client: HikvisionIsapiClient,
    *,
    probe: FfprobeMediaProbe,
    worker_id: str = "archive-worker",
) -> str | None:
    _touch_heartbeat("polling", force=True)
    try:
        job = claim_next_archive_job(session, worker_id=worker_id)
        if job is not None:
            session.commit()
    except OperationalError as exc:
        if not _is_sqlite_lock(exc):
            raise
        # Claiming is durable state, but a transient scanner/API write conflict
        # must not terminate the long-running worker. Roll back the uncommitted
        # claim and let the next polling interval retry it.
        session.rollback()
        return None
    if job is None:
        return None
    lease_token = job.lease_token
    if not lease_token:
        return job.id
    last_lease_renewal = time.monotonic()

    def renew(phase: str, *, force: bool = False) -> None:
        nonlocal last_lease_renewal
        now = time.monotonic()
        if force or now - last_lease_renewal >= 30:
            if not renew_archive_claim(session, job.id, worker_id, lease_token, phase):
                raise RuntimeError("ARCHIVE_JOB_LEASE_LOST")
            last_lease_renewal = now

    source = session.get(SourceRecording, job.source_recording_id)
    tombstoned = session.scalar(
        select(Tombstone.id).where(
            Tombstone.source_recording_id == job.source_recording_id,
            Tombstone.suppress_redownload.is_(True),
        )
    )
    if source is None or source.source_state not in {"available", "expired"} or tombstoned:
        job.state = "failed"
        job.error_code = "SOURCE_NOT_AVAILABLE"
        session.commit()
        return job.id
    archive_committed = False
    try:
        nvr = session.get(NvrDevice, source.nvr_id)
        if nvr is None:
            raise NvrProtocolError("NVR_NOT_REGISTERED")
        volume = select_storage_volume(
            session.scalars(select(StorageVolume)).all(),
            required_bytes=source.source_size_if_known or 0,
        )
        day = source.source_start.strftime("%Y/%m/%d")
        camera = _safe_part(source.track_id)
        stem = f"{source.source_start.strftime('%H%M%S')}-{_safe_part(source.id)}"
        relative_path = f"recordings/{day}/camera-{camera}/{stem}.mp4"
        final_path = Path(volume.mount_path_runtime) / relative_path
        part_path = final_path.with_suffix(final_path.suffix + ".part")
        if final_path.exists():
            # A crash may occur after atomic promotion but before the database
            # transaction commits. Re-verify/adopt this deterministic orphan
            # before asking the NVR for the source or downloading it again.
            job.state = "verifying"
            session.commit()
            renew("verifying", force=True)
            _touch_heartbeat("verifying", force=True)
            finalize_archive_job(
                session,
                job=job,
                source=source,
                storage_volume_id=volume.id,
                relative_path=relative_path,
                part_path=part_path,
                final_path=final_path,
                storage_root=Path(volume.mount_path_runtime),
                probe=probe,
                asset_id=f"asset-{job.id.removeprefix('job-')}",
                verification_duration_ms=source.duration_ms,
                verification_size_bytes=source.source_size_if_known,
            )
            session.commit()
            archive_committed = True
            release_archive_claim(session, job.id, worker_id, lease_token, "completed")
            return job.id
        segment = _find_exact_segment(client, source)
        # A previously expired source becomes available only after the worker
        # has actually resolved a matching NVR segment. Queue creation alone is
        # never allowed to rewrite this fact.
        if source.source_state == "expired":
            _adopt_resolved_segment(source, segment)
            session.commit()
        if part_path.exists():
            quarantine_partial_file(
                session,
                path=part_path,
                storage_root=Path(volume.mount_path_runtime),
                job_id=job.id,
            )
            session.commit()
        job.state = "downloading"
        job.temp_path = str(part_path.relative_to(Path(volume.mount_path_runtime)))
        session.commit()
        last_progress_commit = 0.0

        def report_progress(received: int) -> None:
            nonlocal last_progress_commit
            now = time.monotonic()
            _touch_heartbeat("downloading")
            renew("downloading")
            # Persist at most once per second so the live UI is useful without
            # turning every network chunk into a database write.
            if now - last_progress_commit >= 1.0:
                job.bytes_received = received
                try:
                    session.commit()
                except OperationalError as exc:
                    if not _is_sqlite_lock(exc):
                        raise
                    # Progress is telemetry. A concurrent scanner write must
                    # not abort an otherwise valid NVR download; the next
                    # callback will attempt another progress update.
                    session.rollback()
                last_progress_commit = now

        received = client.download_to_part(
            segment.playback_uri,
            part_path,
            progress_callback=report_progress,
        )
        job.bytes_received = received
        session.commit()

        # Hikvision can return a rolling segment whose playback URI has
        # outgrown the search row between discovery and download. Re-resolve
        # only on a material size mismatch, then accept the payload only when
        # its probed media matches the refreshed NVR segment. This avoids
        # saving a longer file under stale metadata and avoids a second large
        # download.
        if segment.source_size_if_known is not None and abs(received - segment.source_size_if_known) > VerificationPolicy().size_tolerance_bytes:
            try:
                media = probe.probe(part_path)
            except Exception as exc:
                raise ArchiveValidationError("MEDIA_PROBE_FAILED") from exc
            refreshed = _find_exact_segment(client, source, search_horizon=timedelta(days=1))
            if not _media_matches_segment(refreshed, media=media, received=received):
                raise ArchiveValidationError("MEDIA_DURATION_MISMATCH")
            segment = refreshed
            _adopt_resolved_segment(source, segment)
            session.commit()
        # Make the post-download phase visible to the API before the potentially
        # expensive probe/hash work starts.  Without this commit, another API
        # session keeps seeing "downloading" at 100% with no useful speed.
        job.state = "verifying"
        session.commit()
        renew("verifying", force=True)
        _touch_heartbeat("verifying", force=True)
        finalize_archive_job(
            session,
            job=job,
            source=source,
            storage_volume_id=volume.id,
            relative_path=relative_path,
            part_path=part_path,
            final_path=final_path,
            storage_root=Path(volume.mount_path_runtime),
            probe=probe,
            asset_id=f"asset-{job.id.removeprefix('job-')}",
            # The NVR may return the current, longer rolling interval for an
            # older source snapshot. Verify against the exact payload we
            # resolved, while retaining the original requested interval as
            # the source-record identity shown by the UI.
            verification_duration_ms=segment.duration_ms,
            verification_size_bytes=segment.source_size_if_known,
        )
        session.commit()
        archive_committed = True
        release_archive_claim(session, job.id, worker_id, lease_token, "completed")
    except (NvrProtocolError, StorageError, ArchiveValidationError, OSError) as exc:
        if archive_committed:
            session.rollback()
            return job.id
        session.rollback()
        job = session.get(type(job), job.id) or job
        job.state = "failed"
        error_code = getattr(exc, "code", "ARCHIVE_WORKER_FAILED")
        part_path_for_quarantine = locals().get("part_path")
        volume_for_quarantine = locals().get("volume")
        if part_path_for_quarantine is not None and volume_for_quarantine is not None:
            try:
                quarantine_partial_file(
                    session,
                    path=part_path_for_quarantine,
                    storage_root=Path(volume_for_quarantine.mount_path_runtime),
                    job_id=job.id,
                )
            except (OSError, ArchiveValidationError):
                pass
        _record_archive_failure(session, job, error_code=error_code)
        if getattr(exc, "code", None) == "NVR_SOURCE_SEGMENT_NOT_FOUND" and source is not None:
            source.source_state = "expired"
            source.expired_observed_at = datetime.now(timezone.utc)
        session.commit()
        release_archive_claim(session, job.id, worker_id, lease_token, "failed")
    except Exception as exc:
        if archive_committed:
            # The verified archive transaction committed; a later lease-release
            # issue must not turn the durable asset back into a failed archive.
            session.rollback()
            return job.id
        phase_before_rollback = job.lease_phase or job.state
        session.rollback()
        job = session.get(type(job), job.id) or job
        job.state = "failed"
        part_path_for_quarantine = locals().get("part_path")
        volume_for_quarantine = locals().get("volume")
        if part_path_for_quarantine is not None and volume_for_quarantine is not None:
            try:
                quarantine_partial_file(
                    session,
                    path=part_path_for_quarantine,
                    storage_root=Path(volume_for_quarantine.mount_path_runtime),
                    job_id=job.id,
                )
            except (OSError, ArchiveValidationError):
                pass
        error_code = (
            "ARCHIVE_COMMIT_OR_FINALIZE_FAILED"
            if phase_before_rollback == "verifying"
            else "ARCHIVE_WORKER_FAILED"
        )
        _record_archive_failure(
            session,
            job,
            error_code=error_code,
            detail="UNEXPECTED_" + type(exc).__name__.upper(),
        )
        session.commit()
        release_archive_claim(session, job.id, worker_id, lease_token, "failed")
    return job.id


def run(*, once: bool, interval_seconds: int) -> None:
    settings = get_settings()
    if not (settings.nvr_base_url and settings.nvr_username and settings.nvr_password and settings.nvr_id):
        raise SystemExit("NVR runtime credentials are required for archive worker")
    with HikvisionIsapiClient(
        base_url=settings.nvr_base_url,
        username=settings.nvr_username,
        password=settings.nvr_password.get_secret_value(),
        download_method=settings.archive_download_method.upper(),
        timeout_seconds=600,
    ) as client:
        worker_id = os.environ.get("P16_WORKER_ID") or f"archive-worker-{socket.gethostname()}-{os.getpid()}"
        while True:
            with SessionLocal() as session:
                _recover_interrupted_jobs(session)
            with SessionLocal() as session:
                processed = process_one(session, client, probe=FfprobeMediaProbe(), worker_id=worker_id)
            _touch_heartbeat("polling", force=True)
            if once or processed is None:
                if not once:
                    time.sleep(interval_seconds)
                if once:
                    return


def main() -> None:
    parser = argparse.ArgumentParser(description="Home Vision archive worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=get_settings().archive_poll_seconds)
    args = parser.parse_args()
    run(once=args.once, interval_seconds=max(1, args.interval_seconds))


if __name__ == "__main__":
    main()
