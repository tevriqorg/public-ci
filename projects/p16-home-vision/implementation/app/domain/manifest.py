from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.analysis_profile import profile_snapshot_hash_from_input, validate_profile_snapshot
from app.models import (
    AnalysisProfile,
    AnalysisRun,
    ArchiveJob,
    ArchiveSourceSnapshot,
    LocalAsset,
    SourceRecording,
    Tombstone,
    utcnow,
)


def stable_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def canonical_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def upsert_source_recording(
    session: Session,
    *,
    nvr_id: str,
    track_id: str,
    source_start: datetime,
    source_end: datetime,
    duration_ms: int,
    locator_fingerprint: str | None,
    source_size_if_known: int | None = None,
    scan_id: str | None = None,
) -> SourceRecording:
    candidates = session.scalars(
        select(SourceRecording)
        .where(
            SourceRecording.nvr_id == nvr_id,
            SourceRecording.track_id == track_id,
            SourceRecording.source_start == source_start,
        )
        .order_by(SourceRecording.source_end.desc(), SourceRecording.id)
    ).all()
    recording = candidates[0] if candidates else None
    now = utcnow()
    if recording is None:
        recording = SourceRecording(
            id=stable_id("src"),
            nvr_id=nvr_id,
            track_id=track_id,
            source_start=source_start,
            source_end=source_end,
            duration_ms=duration_ms,
            source_size_if_known=source_size_if_known,
            locator_fingerprint=locator_fingerprint,
            source_state="available",
            first_seen_at=now,
            last_seen_at=now,
            last_confirmed_at=now,
            first_scan_id=scan_id,
            last_scan_id=scan_id,
        )
        session.add(recording)
    else:
        stored_end = recording.source_end
        stored_end_utc = stored_end.replace(tzinfo=timezone.utc) if stored_end.tzinfo is None else stored_end.astimezone(timezone.utc)
        incoming_end_utc = source_end.replace(tzinfo=timezone.utc) if source_end.tzinfo is None else source_end.astimezone(timezone.utc)
        if incoming_end_utc >= stored_end_utc:
            recording.source_end = source_end
            recording.duration_ms = duration_ms
            recording.source_size_if_known = source_size_if_known
            recording.locator_fingerprint = locator_fingerprint
        recording.last_seen_at = now
        recording.last_confirmed_at = now
        recording.last_scan_id = scan_id
        if recording.source_state != "expired":
            recording.source_state = "available"
    session.flush()
    return recording


def create_profile_revision(
    session: Session,
    *,
    profile_id: str,
    revision: int,
    name: str,
    payload: dict[str, Any],
) -> AnalysisProfile:
    snapshot_hash = profile_snapshot_hash_from_input(name=name, payload=payload)
    dump = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    profile = AnalysisProfile(
        id=profile_id,
        revision=revision,
        name=name,
        enabled=bool(payload.get("enabled", True)),
        camera_scope_json=dump(payload.get("camera_scope")) if "camera_scope" in payload else None,
        time_scope_json=dump(payload.get("time_scope")) if "time_scope" in payload else None,
        roi_json=dump(payload.get("roi")) if "roi" in payload else None,
        object_interests_json=dump(payload.get("object_interests")) if "object_interests" in payload else None,
        screening_policy_json=dump(payload.get("screening_policy")) if "screening_policy" in payload else None,
        candidate_policy_json=dump(payload.get("candidate_policy")) if "candidate_policy" in payload else None,
        semantic_intent=payload.get("semantic_intent"),
        prompt_schema_json=dump(payload.get("prompt_schema")) if "prompt_schema" in payload else None,
        preferred_model_class=payload.get("preferred_model_class"),
        snapshot_hash=snapshot_hash,
    )
    session.add(profile)
    session.flush()
    return profile


def create_analysis_run(
    session: Session,
    *,
    run_id: str,
    asset_id: str,
    profile: AnalysisProfile,
    pipeline_version: str,
) -> AnalysisRun:
    validate_profile_snapshot(profile)
    run = AnalysisRun(
        id=run_id,
        asset_id=asset_id,
        analysis_profile_id=profile.id,
        analysis_profile_revision=profile.revision,
        analysis_profile_snapshot_hash=profile.snapshot_hash,
        pipeline_version=pipeline_version,
        state="pending",
    )
    session.add(run)
    session.flush()
    return run


def _archive_status_for_source(
    source: SourceRecording | None,
    assets: list[LocalAsset],
    snapshots: dict[str, ArchiveSourceSnapshot],
    tombstone: Tombstone | None,
    job: ArchiveJob | None,
) -> str:
    if tombstone:
        return "deleted_by_user"
    if source is None:
        return "unknown"
    source_assets = [a for a in assets if a.asset_role == "source_archive"]
    # Playback URIs can change with the NVR address while the archived segment stays identical.
    current_source = (
        source.nvr_id, source.track_id, source.source_start, source.source_end,
        source.duration_ms, source.source_size_if_known,
    )
    def matches_current(asset: LocalAsset) -> bool:
        snapshot = snapshots.get(asset.archive_source_snapshot_id)
        return snapshot is not None and (
            snapshot.nvr_id, snapshot.track_id, snapshot.source_start, snapshot.source_end,
            snapshot.duration_ms, snapshot.source_size_if_known,
        ) == current_source

    verified_source_assets = [
        a for a in source_assets
        if a.lifecycle_state == "present" and a.verification_state == "verified"
    ]
    if any(matches_current(a) for a in verified_source_assets):
        return "archived_verified"
    if any(a.archive_source_snapshot_id is None for a in verified_source_assets):
        return "archived_legacy_unbound"
    if verified_source_assets:
        return "archived_older_snapshot"
    if any(a.lifecycle_state == "present" and a.verification_state == "failed" for a in source_assets):
        return "archived_invalid"
    if any(
        a.lifecycle_state == "present" and a.verification_state == "verified"
        for a in assets if a.asset_role == "normalized_playback"
    ):
        return "normalized_only"

    if job and job.state in {"queued", "resolving_source", "downloading", "verifying", "paused"}:
        return job.state

    if source.source_state == "expired":
        return "source_expired_unarchived"
    if source.source_state == "available":
        return "not_archived"
    return "unknown"


def effective_archive_status(session: Session, source_recording_id: str) -> str:
    return effective_archive_statuses(session, [source_recording_id]).get(source_recording_id, "unknown")


def effective_archive_statuses(session: Session, source_recording_ids: Iterable[str]) -> dict[str, str]:
    source_ids = set(source_recording_ids)
    if not source_ids:
        return {}

    sources = {
        source.id: source
        for source in session.scalars(
            select(SourceRecording).where(SourceRecording.id.in_(source_ids))
        ).all()
    }
    tombstones = {}
    for tombstone in session.scalars(
        select(Tombstone)
        .where(
            Tombstone.source_recording_id.in_(source_ids),
            Tombstone.suppress_redownload.is_(True),
        )
        .order_by(Tombstone.deleted_at.desc())
    ).all():
        tombstones.setdefault(tombstone.source_recording_id, tombstone)

    assets_by_source: dict[str, list[LocalAsset]] = {}
    snapshot_ids: set[str] = set()
    for asset in session.scalars(
        select(LocalAsset).where(LocalAsset.source_recording_id.in_(source_ids))
    ).all():
        assets_by_source.setdefault(asset.source_recording_id, []).append(asset)
        if asset.asset_role == "source_archive" and asset.archive_source_snapshot_id:
            snapshot_ids.add(asset.archive_source_snapshot_id)
    snapshots = {
        snapshot.id: snapshot
        for snapshot in session.scalars(
            select(ArchiveSourceSnapshot).where(ArchiveSourceSnapshot.id.in_(snapshot_ids))
        ).all()
    } if snapshot_ids else {}

    jobs = {}
    for job in session.scalars(
        select(ArchiveJob)
        .where(ArchiveJob.source_recording_id.in_(source_ids))
        .order_by(ArchiveJob.requested_at.desc())
    ).all():
        jobs.setdefault(job.source_recording_id, job)

    return {
        source_id: _archive_status_for_source(
            sources.get(source_id),
            assets_by_source.get(source_id, []),
            snapshots,
            tombstones.get(source_id),
            jobs.get(source_id),
        )
        for source_id in source_ids
    }
