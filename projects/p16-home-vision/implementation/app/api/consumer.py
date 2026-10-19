from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.db import get_session
from app.config import get_settings
from app.api.r2_assets import asset_projection, ready_web_media
from app.domain.media_access import decide_media_access, resolve_media_path
from app.domain.manifest import effective_archive_statuses
from app.domain.thumbnails import source_thumbnail_path, thumbnail_path
from app.models import (
    AnalysisArtifact,
    AnalysisEvent,
    AnalysisRun,
    AnalysisProfile,
    CameraTrack,
    LocalAsset,
    ArchiveJob,
    AuditEvent,
    ReviewFlag,
    ScanRun,
    SourceRecording,
    StorageVolume,
)

router = APIRouter(prefix="/api/v1", tags=["consumer"])
T = TypeVar("T")


def _page(statement: Any, model: type[T], *, after_id: str | None, limit: int) -> Any:
    if after_id:
        statement = statement.where(model.id > after_id)
    return statement.order_by(model.id).limit(limit)


def _list_response(items: list[dict[str, Any]], *, next_after_id: str | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"items": items, "count": len(items)}
    if next_after_id:
        response["next_after_id"] = next_after_id
    return response


def _parse_json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _camera_payload(camera: CameraTrack) -> dict[str, Any]:
    return {
        "camera_id": camera.id,
        "nvr_id": camera.nvr_id,
        "track_id": camera.track_id,
        "name": camera.camera_name,
        "stream_role": camera.stream_role,
        "enabled": camera.enabled,
    }


def _normalization_progress(asset: LocalAsset, volume: StorageVolume | None, *, ready: bool) -> float | None:
    if ready:
        return 100
    if volume is None or asset.size_bytes is None or asset.size_bytes <= 0:
        return 0 if asset.verification_state == "verified" and asset.lifecycle_state == "present" else None
    try:
        part_path = resolve_media_path(Path(volume.mount_path_runtime), f"recordings/normalized/{asset.id}.mp4.part")
        part_bytes = part_path.stat().st_size if part_path.is_file() else 0
    except (OSError, ValueError):
        part_bytes = 0
    return min(99, round(part_bytes * 100 / asset.size_bytes, 1))


def _normalization_state(asset: LocalAsset, *, ready: bool) -> str | None:
    if ready:
        return "completed"
    if asset.asset_role != "source_archive" or asset.verification_state != "verified" or asset.lifecycle_state != "present":
        return None
    if asset.normalization_phase in {"failed", "completed"}:
        return "failed"
    lease_expires_at = asset.normalization_lease_expires_at
    if lease_expires_at is not None:
        if lease_expires_at.tzinfo is None:
            lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)
        if lease_expires_at > datetime.now(timezone.utc):
            return "transcoding" if asset.normalization_phase in {"processing", "verifying"} else "initializing"
    return "pending"


def _asset_payload(
    asset: LocalAsset,
    *,
    normalization_ready: bool = False,
    normalization_progress: float | None = None,
) -> dict[str, Any]:
    return {
        "asset_id": asset.id,
        "source_recording_id": asset.source_recording_id,
        "asset_role": asset.asset_role,
        "verification_state": asset.verification_state,
        "lifecycle_state": asset.lifecycle_state,
        "size_bytes": asset.size_bytes,
        "sha256": asset.sha256,
        "container": asset.container,
        "video_codec": asset.video_codec,
        "audio_codec": asset.audio_codec,
        "width": asset.width,
        "height": asset.height,
        "media_duration_ms": asset.media_duration_ms,
        "verified_at": asset.verified_at,
        "archived_at": asset.archived_at,
        "deleted_at": asset.deleted_at,
        "normalization_state": _normalization_state(asset, ready=normalization_ready),
        "normalization_error_code": asset.normalization_error_code,
        "normalization_progress_percent": normalization_progress,
        **asset_projection(asset.id),
    }


def _recording_payload(recording: SourceRecording, *, archive_status: str | None = None) -> dict[str, Any]:
    source_thumbnail = source_thumbnail_path(
        get_settings().data_root.parent,
        recording.id,
        recording.locator_fingerprint,
    )
    try:
        thumbnail_available = source_thumbnail.is_file() and source_thumbnail.stat().st_size > 0
    except OSError:
        thumbnail_available = False
    return {
        "source_recording_id": recording.id,
        "nvr_id": recording.nvr_id,
        "track_id": recording.track_id,
        "source_start": recording.source_start,
        "source_end": recording.source_end,
        "duration_ms": recording.duration_ms,
        "source_size_if_known": recording.source_size_if_known,
        "content_type": recording.content_type,
        "recording_type": recording.recording_type,
        "source_state": recording.source_state,
        "last_confirmed_at": recording.last_confirmed_at,
        "thumbnail_available": thumbnail_available,
        "archive_status": archive_status,
    }


def _job_payload(
    session: Session,
    job: ArchiveJob,
    *,
    expected_bytes: int | None = None,
    lookup_source: bool = True,
) -> dict[str, Any]:
    if lookup_source:
        source = session.get(SourceRecording, job.source_recording_id)
        expected_bytes = source.source_size_if_known if source is not None else None
    progress_percent = 100 if job.state == "completed" else None
    if progress_percent is None and expected_bytes and expected_bytes > 0:
        progress_percent = min(100, round((job.bytes_received or 0) * 100 / expected_bytes, 1))
        if job.state == "downloading":
            progress_percent = min(99, progress_percent)
    throughput_bytes_per_second = None
    if job.state == "downloading" and job.started_at and (job.bytes_received or 0) > 0:
        started_at = job.started_at.replace(tzinfo=timezone.utc) if job.started_at.tzinfo is None else job.started_at
        elapsed_seconds = max(0.0, (datetime.now(timezone.utc) - started_at.astimezone(timezone.utc)).total_seconds())
        if elapsed_seconds > 0:
            throughput_bytes_per_second = round(job.bytes_received / elapsed_seconds, 1)
    return {
        "archive_job_id": job.id,
        "source_recording_id": job.source_recording_id,
        "requested_by": job.requested_by,
        "requested_at": job.requested_at,
        "state": job.state,
        "attempt_count": job.attempt_count,
        "bytes_received": job.bytes_received,
        "source_size_bytes": expected_bytes,
        "progress_percent": progress_percent,
        "throughput_bytes_per_second": throughput_bytes_per_second,
        "phase": job.state,
        "error_present": bool(job.error_code or job.error_detail_redacted),
        "error_code": job.error_code,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
    }


def _volume_payload(session: Session, volume: StorageVolume) -> dict[str, Any]:
    application_used_bytes = session.scalar(
        select(func.coalesce(func.sum(LocalAsset.size_bytes), 0)).where(
            LocalAsset.storage_volume_id == volume.id,
            LocalAsset.verification_state == "verified",
            LocalAsset.lifecycle_state == "present",
        )
    ) or 0
    physical_device_id = volume.physical_device_id
    if not physical_device_id:
        physical_device_id, _writable = _mount_identity(volume.mount_path_runtime)
    storage_identity = (
        f"OMV 归档卷 {volume.id} · {physical_device_id}"
        if physical_device_id
        else f"OMV 归档卷 {volume.id} · 物理设备未登记"
    )
    return {
        "storage_volume_id": volume.id,
        "node_id": volume.node_id,
        "storage_identity": storage_identity,
        "physical_device_id": physical_device_id,
        "total_bytes": volume.total_bytes,
        "free_bytes": volume.free_bytes,
        "application_used_bytes": application_used_bytes,
        "online": volume.online,
        "writable": volume.writable,
        "last_checked_at": volume.last_checked_at,
    }


def _mount_identity(path: str) -> tuple[str | None, bool]:
    try:
        mountinfo = open("/proc/self/mountinfo", encoding="utf-8")
    except OSError:
        return None, False
    with mountinfo:
        for line in mountinfo:
            before_separator, separator, after_separator = line.partition(" - ")
            if not separator:
                continue
            fields = before_separator.split()
            if len(fields) < 6:
                continue
            mount_point = fields[4].replace(r"\040", " ").replace(r"\011", "\t").replace(r"\134", "\\")
            if mount_point != path:
                continue
            source_fields = after_separator.split()
            source = source_fields[1] if len(source_fields) > 1 else None
            return source, "rw" in fields[5].split(",")
    return None, False


def _preferred_playback_asset(session: Session, asset: LocalAsset | None) -> LocalAsset | None:
    """Resolve a verified normalized sidecar while retaining the source ID in URLs."""
    if asset is None or asset.asset_role != "source_archive":
        return asset
    normalized = session.scalar(
        select(LocalAsset)
        .where(
            LocalAsset.source_recording_id == asset.source_recording_id,
            LocalAsset.asset_role == "normalized_playback",
            LocalAsset.verification_state == "verified",
            LocalAsset.lifecycle_state == "present",
        )
        .order_by(LocalAsset.verified_at.desc(), LocalAsset.id.desc())
    )
    return normalized or asset


def _normalization_pending(requested: LocalAsset | None, resolved: LocalAsset | None) -> bool:
    return bool(
        requested is not None
        and requested.asset_role == "source_archive"
        and resolved is not None
        and resolved.id == requested.id
    )


def _runtime_volume_payload(volume_id: str, path: str, node_id: str) -> dict[str, Any] | None:
    try:
        stats = os.statvfs(path)
    except OSError:
        return None
    physical_device_id, writable = _mount_identity(path)
    storage_identity = (
        f"OMV 归档卷 {volume_id} · {physical_device_id}"
        if physical_device_id
        else f"OMV 归档卷 {volume_id} · 物理设备未登记"
    )
    return {
        "storage_volume_id": volume_id,
        "node_id": node_id,
        "storage_identity": storage_identity,
        "physical_device_id": physical_device_id,
        "total_bytes": stats.f_blocks * stats.f_frsize,
        "free_bytes": stats.f_bavail * stats.f_frsize,
        "application_used_bytes": 0,
        "online": True,
        "writable": writable,
        "last_checked_at": datetime.now(timezone.utc),
    }


def _scan_payload(scan: ScanRun) -> dict[str, Any]:
    return {
        "scan_run_id": scan.id,
        "nvr_id": scan.nvr_id,
        "track_id": scan.track_id,
        "query_start": scan.query_start,
        "query_end": scan.query_end,
        "state": scan.state,
        "result_count": scan.result_count,
        "last_checked_at": scan.completed_at or scan.started_at,
        "error_present": bool(scan.error_code),
        "error_code": scan.error_code,
    }


def _review_payload(flag: ReviewFlag) -> dict[str, Any]:
    return {
        "review_flag_id": flag.id,
        "asset_id": flag.asset_id,
        "flag_type": flag.flag_type,
        "confidence": flag.confidence,
        "producer": flag.producer,
        "review_state": flag.review_state,
        "created_at": flag.created_at,
        "reviewed_at": flag.reviewed_at,
    }


def _audit_payload(event: AuditEvent) -> dict[str, Any]:
    return {
        "audit_event_id": event.id,
        "event_type": event.event_type,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "actor": event.actor,
        "created_at": event.created_at,
    }


def _run_payload(run: AnalysisRun) -> dict[str, Any]:
    return {
        "analysis_run_id": run.id,
        "asset_id": run.asset_id,
        "analysis_profile_id": run.analysis_profile_id,
        "analysis_profile_revision": run.analysis_profile_revision,
        "analysis_profile_snapshot_hash": run.analysis_profile_snapshot_hash,
        "pipeline_version": run.pipeline_version,
        "model_id": run.model_id,
        "model_version": run.model_version,
        "runtime_backend": run.runtime_backend,
        "state": run.state,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "error_present": bool(run.error),
    }


def _profile_payload(profile: AnalysisProfile) -> dict[str, Any]:
    return {
        "analysis_profile_id": profile.id,
        "revision": profile.revision,
        "name": profile.name,
        "enabled": profile.enabled,
        "semantic_intent": profile.semantic_intent,
        "preferred_model_class": profile.preferred_model_class,
        "snapshot_hash": profile.snapshot_hash,
        "updated_at": profile.updated_at,
    }


def _artifact_payload(artifact: AnalysisArtifact) -> dict[str, Any]:
    return {
        "artifact_id": artifact.id,
        "analysis_run_id": artifact.analysis_run_id,
        "asset_id": artifact.asset_id,
        "artifact_type": artifact.artifact_type,
        "offset_or_range": _parse_json(artifact.offset_or_range_json),
        "created_at": artifact.created_at,
    }


def _event_payload(session: Session, event: AnalysisEvent) -> dict[str, Any]:
    artifact_ids = list(
        session.scalars(
            select(AnalysisArtifact.id)
            .where(
                AnalysisArtifact.analysis_run_id == event.analysis_run_id,
                AnalysisArtifact.asset_id == event.asset_id,
            )
            .order_by(AnalysisArtifact.id)
        ).all()
    )
    return {
        "event_id": event.id,
        "analysis_run_id": event.analysis_run_id,
        "asset_id": event.asset_id,
        "start_offset_ms": event.start_offset_ms,
        "end_offset_ms": event.end_offset_ms,
        "event_type": event.event_type,
        "summary": event.summary,
        "confidence": event.confidence,
        "artifact_ids": artifact_ids,
        "created_at": event.created_at,
    }


@router.get("/capabilities")
def capabilities() -> dict[str, Any]:
    return {
        "api_version": "v1",
        "mode": "consumer_query_only",
        "consumer_surface": "read_only",
        "internal_control_surface": "local_runtime_only",
        "resources": ["cameras", "recordings", "scan_runs", "assets", "archive_jobs", "storage_volumes", "runtime_status", "analysis_profiles", "analysis_runs", "events", "review_flags", "audit_events", "artifacts", "controlled_media"],
        "outbound_push": False,
        "inbound_analysis_policy": False,
        "stable_external_routing": "p13_gateway_required",
    }


@router.get("/cameras")
def list_cameras(
    after_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rows = session.scalars(_page(select(CameraTrack), CameraTrack, after_id=after_id, limit=limit)).all()
    return _list_response([_camera_payload(row) for row in rows], next_after_id=rows[-1].id if len(rows) == limit else None)


@router.get("/cameras/{camera_id}")
def get_camera(camera_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    row = session.get(CameraTrack, camera_id)
    if row is None:
        raise HTTPException(status_code=404, detail="camera_not_found")
    return _camera_payload(row)


@router.get("/assets")
def list_assets(
    after_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    # Normalized sidecars are implementation details of the same recording.
    # The consumer list keeps one card per archived source; playback resolves
    # the verified sidecar below without exposing duplicate cards.
    rows = session.scalars(
        _page(
            select(LocalAsset).where(LocalAsset.asset_role == "source_archive"),
            LocalAsset,
            after_id=after_id,
            limit=limit,
        )
    ).all()
    source_ids = [row.source_recording_id for row in rows]
    normalized_ids = set(session.scalars(
        select(LocalAsset.source_recording_id).where(
            LocalAsset.source_recording_id.in_(source_ids),
            LocalAsset.asset_role == "normalized_playback",
            LocalAsset.verification_state == "verified",
            LocalAsset.lifecycle_state == "present",
        )
    ).all()) if source_ids else set()
    volumes = {volume.id: volume for volume in session.scalars(
        select(StorageVolume).where(StorageVolume.id.in_({row.storage_volume_id for row in rows}))
    ).all()} if rows else {}
    return _list_response(
        [
            _asset_payload(
                row,
                normalization_ready=row.source_recording_id in normalized_ids,
                normalization_progress=_normalization_progress(
                    row,
                    volumes.get(row.storage_volume_id),
                    ready=row.source_recording_id in normalized_ids,
                ),
            )
            for row in rows
        ],
        next_after_id=rows[-1].id if len(rows) == limit else None,
    )


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    row = session.get(LocalAsset, asset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="asset_not_found")
    normalized = session.scalar(
        select(LocalAsset.id).where(
            LocalAsset.source_recording_id == row.source_recording_id,
            LocalAsset.asset_role == "normalized_playback",
            LocalAsset.verification_state == "verified",
            LocalAsset.lifecycle_state == "present",
        )
    )
    volume = session.get(StorageVolume, row.storage_volume_id)
    return _asset_payload(
        row,
        normalization_ready=normalized is not None,
        normalization_progress=_normalization_progress(row, volume, ready=normalized is not None),
    )


@router.get("/recordings")
def list_recordings(
    after_id: str | None = None,
    limit: int = Query(100, ge=1, le=2000),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    ranked = select(
        SourceRecording.id.label("recording_id"),
        func.row_number().over(
            partition_by=(SourceRecording.nvr_id, SourceRecording.track_id, SourceRecording.source_start),
            order_by=(SourceRecording.source_end.desc(), SourceRecording.id),
        ).label("recording_rank"),
    ).subquery()
    canonical_ids = select(ranked.c.recording_id).where(ranked.c.recording_rank == 1)
    rows = session.scalars(
        _page(select(SourceRecording).where(SourceRecording.id.in_(canonical_ids)), SourceRecording, after_id=after_id, limit=limit)
    ).all()
    archive_statuses = effective_archive_statuses(session, [row.id for row in rows])
    return _list_response(
        [_recording_payload(row, archive_status=archive_statuses.get(row.id, "unknown")) for row in rows],
        next_after_id=rows[-1].id if len(rows) == limit else None,
    )


@router.get("/scan-runs")
def list_scan_runs(
    after_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Expose scan coverage so a UI can distinguish unknown from no-recording."""
    rows = session.scalars(_page(select(ScanRun), ScanRun, after_id=after_id, limit=limit)).all()
    return _list_response([_scan_payload(row) for row in rows], next_after_id=rows[-1].id if len(rows) == limit else None)


@router.get("/archive-jobs")
def list_archive_jobs(
    after_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rows = session.scalars(_page(select(ArchiveJob), ArchiveJob, after_id=after_id, limit=limit)).all()
    expected_sizes = {
        source_id: size
        for source_id, size in session.execute(
            select(SourceRecording.id, SourceRecording.source_size_if_known).where(
                SourceRecording.id.in_({row.source_recording_id for row in rows})
            )
        )
    } if rows else {}
    payloads = [
        _job_payload(
            session,
            row,
            expected_bytes=expected_sizes.get(row.source_recording_id),
            lookup_source=False,
        )
        for row in rows
    ]
    return _list_response(payloads, next_after_id=rows[-1].id if len(rows) == limit else None)


@router.get("/archive-jobs/{job_id}")
def get_archive_job(job_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    row = session.get(ArchiveJob, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="archive_job_not_found")
    return _job_payload(session, row)


@router.get("/storage-volumes")
def list_storage_volumes(
    after_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rows = session.scalars(_page(select(StorageVolume), StorageVolume, after_id=after_id, limit=limit)).all()
    payloads = [_volume_payload(session, row) for row in rows]
    known_ids = {item["storage_volume_id"] for item in payloads}
    fallback_node_id = payloads[0]["node_id"] if payloads else "runtime-host"
    for volume_id, path in (("archive-a", "/archive/a"), ("archive-b", "/archive/b")):
        if volume_id not in known_ids:
            runtime_volume = _runtime_volume_payload(volume_id, path, fallback_node_id)
            if runtime_volume is not None:
                payloads.append(runtime_volume)
    payloads.sort(key=lambda item: item["storage_volume_id"])
    if after_id:
        payloads = [item for item in payloads if item["storage_volume_id"] > after_id]
    payloads = payloads[:limit]
    return _list_response(payloads, next_after_id=payloads[-1]["storage_volume_id"] if len(payloads) == limit else None)


@router.get("/review-flags")
def list_review_flags(
    after_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rows = session.scalars(_page(select(ReviewFlag), ReviewFlag, after_id=after_id, limit=limit)).all()
    return _list_response([_review_payload(row) for row in rows], next_after_id=rows[-1].id if len(rows) == limit else None)


@router.get("/audit-events")
def list_audit_events(
    after_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rows = session.scalars(_page(select(AuditEvent), AuditEvent, after_id=after_id, limit=limit)).all()
    return _list_response([_audit_payload(row) for row in rows], next_after_id=rows[-1].id if len(rows) == limit else None)


@router.get("/analysis-runs")
def list_analysis_runs(
    after_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rows = session.scalars(_page(select(AnalysisRun), AnalysisRun, after_id=after_id, limit=limit)).all()
    return _list_response([_run_payload(row) for row in rows], next_after_id=rows[-1].id if len(rows) == limit else None)


@router.get("/analysis-profiles")
def list_analysis_profiles(session: Session = Depends(get_session)) -> dict[str, Any]:
    rows = session.scalars(select(AnalysisProfile).order_by(AnalysisProfile.id, AnalysisProfile.revision)).all()
    return _list_response([_profile_payload(row) for row in rows])


@router.get("/analysis-runs/{run_id}")
def get_analysis_run(run_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    row = session.get(AnalysisRun, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="analysis_run_not_found")
    return _run_payload(row)


@router.get("/events")
def list_events(
    after_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rows = session.scalars(_page(select(AnalysisEvent), AnalysisEvent, after_id=after_id, limit=limit)).all()
    return _list_response([_event_payload(session, row) for row in rows], next_after_id=rows[-1].id if len(rows) == limit else None)


@router.get("/events/{event_id}")
def get_event(event_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    row = session.get(AnalysisEvent, event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="event_not_found")
    return _event_payload(session, row)


@router.get("/artifacts")
def list_artifacts(
    after_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rows = session.scalars(_page(select(AnalysisArtifact), AnalysisArtifact, after_id=after_id, limit=limit)).all()
    return _list_response([_artifact_payload(row) for row in rows], next_after_id=rows[-1].id if len(rows) == limit else None)


@router.get("/media/{asset_id}/access")
def get_media_access(asset_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    requested_asset = session.get(LocalAsset, asset_id)
    if requested_asset is not None:
        source_volume = session.get(StorageVolume, requested_asset.storage_volume_id)
        if decide_media_access(requested_asset, source_volume).status == "available" and ready_web_media(asset_id) is not None:
            return {
                "asset_id": asset_id,
                "status": "available",
                "playback_path": f"/api/r2/assets/{asset_id}/web",
                "playback_mode": "r2_web_media",
                "container": "mp4",
            }
    asset = _preferred_playback_asset(session, requested_asset)
    if _normalization_pending(requested_asset, asset):
        return {"asset_id": asset_id, "status": "normalization_pending", "normalization_state": "transcoding"}
    volume = session.get(StorageVolume, asset.storage_volume_id) if asset is not None else None
    decision = decide_media_access(asset, volume)
    payload: dict[str, Any] = {"asset_id": asset_id, "status": decision.status}
    if decision.status == "available":
        payload["content_path"] = f"/api/v1/media/{asset_id}/content"
        payload["playback_path"] = f"/api/v1/media/{asset_id}/playback"
        payload["playback_mode"] = "normalized" if asset.id != asset_id else "original"
        if asset.id != asset_id:
            payload["normalized_asset_id"] = asset.id
        payload["video_codec"] = asset.video_codec
        payload["audio_codec"] = asset.audio_codec
        payload["container"] = asset.container
    return payload


@router.get("/media/{asset_id}/content")
def get_media_content(asset_id: str, session: Session = Depends(get_session)) -> FileResponse:
    requested_asset = session.get(LocalAsset, asset_id)
    asset = _preferred_playback_asset(session, requested_asset)
    if _normalization_pending(requested_asset, asset):
        raise HTTPException(status_code=503, detail="asset_normalization_pending")
    volume = session.get(StorageVolume, asset.storage_volume_id) if asset is not None else None
    decision = decide_media_access(asset, volume)
    if decision.status == "not_found":
        raise HTTPException(status_code=404, detail="asset_not_found")
    if decision.status == "deleted":
        raise HTTPException(status_code=410, detail="asset_deleted")
    if decision.status != "available" or decision.path is None:
        raise HTTPException(status_code=503, detail="asset_temporarily_unavailable")
    media_type = "video/mp4" if (asset.container or "").lower() in {"mp4", "mov"} else "video/mpeg" if (asset.container or "").lower() == "mpeg" else "application/octet-stream"
    return FileResponse(decision.path, media_type=media_type)


@router.get("/media/{asset_id}/playback", response_model=None)
def get_media_playback(asset_id: str, session: Session = Depends(get_session)) -> FileResponse:
    """Serve a browser-seekable file without rewriting the verified archive."""
    requested_asset = session.get(LocalAsset, asset_id)
    asset = _preferred_playback_asset(session, requested_asset)
    if _normalization_pending(requested_asset, asset):
        raise HTTPException(status_code=503, detail="asset_normalization_pending")
    volume = session.get(StorageVolume, asset.storage_volume_id) if asset is not None else None
    decision = decide_media_access(asset, volume)
    if decision.status == "not_found":
        raise HTTPException(status_code=404, detail="asset_not_found")
    if decision.status == "deleted":
        raise HTTPException(status_code=410, detail="asset_deleted")
    if decision.status != "available" or decision.path is None or asset is None:
        raise HTTPException(status_code=503, detail="asset_temporarily_unavailable")
    headers = {"Content-Disposition": "inline", "Cache-Control": "private, max-age=3600"}
    media_type = "video/mp4" if (asset.container or "").lower() in {"mp4", "mov"} else "video/mpeg" if (asset.container or "").lower() == "mpeg" else "application/octet-stream"
    return FileResponse(decision.path, media_type=media_type, headers=headers)


@router.get("/media/{asset_id}/thumbnail")
def get_media_thumbnail(asset_id: str, session: Session = Depends(get_session)) -> FileResponse:
    """Serve the pre-generated same-volume thumbnail; never generate on demand."""
    asset = session.get(LocalAsset, asset_id)
    volume = session.get(StorageVolume, asset.storage_volume_id) if asset is not None else None
    if asset is None:
        raise HTTPException(status_code=404, detail="asset_not_found")
    if asset.lifecycle_state == "deleted":
        raise HTTPException(status_code=410, detail="asset_deleted")
    if asset.lifecycle_state != "present" or asset.verification_state != "verified" or volume is None or not volume.online:
        raise HTTPException(status_code=503, detail="asset_temporarily_unavailable")
    saved_path = thumbnail_path(Path(volume.mount_path_runtime), asset_id)
    if not saved_path.is_file() or saved_path.stat().st_size == 0:
        raise HTTPException(status_code=404, detail="thumbnail_not_generated")
    return FileResponse(saved_path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/source-recordings/{source_recording_id}/thumbnail")
def get_source_recording_thumbnail(source_recording_id: str, session: Session = Depends(get_session)) -> FileResponse:
    recording = session.get(SourceRecording, source_recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="source_recording_not_found")
    saved_path = source_thumbnail_path(
        get_settings().data_root.parent,
        recording.id,
        recording.locator_fingerprint,
    )
    if not saved_path.is_file() or saved_path.stat().st_size == 0:
        raise HTTPException(status_code=404, detail="thumbnail_not_generated")
    return FileResponse(saved_path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
