from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ArchiveJob, CameraTrack, LocalAsset, ScanRun, SourceRecording, StorageVolume, Tombstone


@dataclass(frozen=True)
class ArchiveFinding:
    code: str
    severity: str
    entity_id: str
    relative_path: str | None = None


def _safe_asset_path(volume: StorageVolume, relative_path: str) -> Path | None:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    root = Path(volume.mount_path_runtime)
    try:
        resolved_root = root.resolve(strict=False)
        resolved = (resolved_root / candidate).resolve(strict=False)
        if not resolved.is_relative_to(resolved_root):
            return None
        return resolved
    except OSError:
        return None


def audit_archive_state(
    session: Session,
    *,
    now: datetime | None = None,
    include_file_orphans: bool = False,
) -> dict:
    """Produce a read-only coverage, ledger and storage reconciliation report."""
    now = now or datetime.now(timezone.utc)
    findings: list[ArchiveFinding] = []
    volumes = {volume.id: volume for volume in session.scalars(select(StorageVolume)).all()}
    assets = list(session.scalars(select(LocalAsset)).all())
    known_files: dict[str, set[str]] = {volume_id: set() for volume_id in volumes}

    for volume in volumes.values():
        root = Path(volume.mount_path_runtime)
        if not root.exists() or not root.is_dir():
            findings.append(ArchiveFinding("VOLUME_PATH_MISSING", "error", volume.id))
        if not volume.online:
            findings.append(ArchiveFinding("VOLUME_RECORDED_OFFLINE", "warning", volume.id))

    for asset in assets:
        volume = volumes.get(asset.storage_volume_id)
        if volume is None:
            findings.append(ArchiveFinding("ASSET_VOLUME_UNKNOWN", "error", asset.id, asset.relative_path))
            continue
        path = _safe_asset_path(volume, asset.relative_path)
        if path is None:
            findings.append(ArchiveFinding("ASSET_PATH_UNSAFE", "error", asset.id, asset.relative_path))
            continue
        known_files[volume.id].add(asset.relative_path)
        if asset.lifecycle_state == "present" and not path.is_file():
            findings.append(ArchiveFinding("DB_ASSET_FILE_MISSING", "error", asset.id, asset.relative_path))
        elif path.is_file() and asset.size_bytes is not None and path.stat().st_size != asset.size_bytes:
            findings.append(ArchiveFinding("DB_ASSET_SIZE_MISMATCH", "error", asset.id, asset.relative_path))

    active_states = {"queued", "resolving_source", "downloading", "verifying", "paused"}
    active_jobs = list(session.scalars(select(ArchiveJob).where(ArchiveJob.state.in_(active_states))).all())
    for job in active_jobs:
        if job.lease_expires_at is not None:
            expiry = job.lease_expires_at
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= now:
                findings.append(ArchiveFinding("STALE_ARCHIVE_LEASE", "warning", job.id))
        if job.temp_path:
            candidates = [
                path
                for volume in volumes.values()
                if (path := _safe_asset_path(volume, job.temp_path)) is not None
            ]
            if candidates and not any(path.is_file() for path in candidates):
                findings.append(ArchiveFinding("JOB_PART_FILE_MISSING", "warning", job.id, job.temp_path))

    cameras = list(session.scalars(select(CameraTrack).where(CameraTrack.enabled.is_(True))).all())
    coverage = []
    for camera in cameras:
        latest = session.scalar(
            select(func.max(ScanRun.completed_at)).where(
                ScanRun.nvr_id == camera.nvr_id,
                ScanRun.track_id == camera.track_id,
                ScanRun.state == "success",
            )
        )
        failed = session.scalar(
            select(func.max(ScanRun.completed_at)).where(
                ScanRun.nvr_id == camera.nvr_id,
                ScanRun.track_id == camera.track_id,
                ScanRun.state == "failed",
            )
        )
        if latest is None:
            findings.append(ArchiveFinding("CAMERA_NO_SUCCESSFUL_SCAN", "warning", camera.id))
        coverage.append({
            "nvr_id": camera.nvr_id,
            "track_id": camera.track_id,
            "last_successful_scan_at": latest.isoformat() if latest else None,
            "last_failed_scan_at": failed.isoformat() if failed else None,
        })

    active_source_ids = {job.source_recording_id for job in active_jobs}
    archived_source_ids = set(
        session.scalars(
            select(LocalAsset.source_recording_id).where(
                LocalAsset.asset_role == "source_archive",
                LocalAsset.verification_state == "verified",
                LocalAsset.lifecycle_state == "present",
                LocalAsset.archive_source_snapshot_id.is_not(None),
            )
        ).all()
    )
    suppressed_source_ids = set(
        session.scalars(
            select(Tombstone.source_recording_id).where(Tombstone.suppress_redownload.is_(True))
        ).all()
    )
    backlog = [
        source
        for source in session.scalars(
            select(SourceRecording)
            .where(SourceRecording.source_state == "available")
            .order_by(SourceRecording.source_start.asc())
        ).all()
        if source.id not in active_source_ids
        and source.id not in archived_source_ids
        and source.id not in suppressed_source_ids
    ]
    result = {
        "schema_name": "p16.archive.reconciliation",
        "schema_version": 1,
        "created_at": now.isoformat(),
        "read_only": True,
        "automatic_repairs": False,
        "coverage": coverage,
        "storage": [
            {
                "volume_id": volume.id,
                "recorded_online": volume.online,
                "path_available": Path(volume.mount_path_runtime).is_dir(),
                "recorded_free_bytes": volume.free_bytes,
                "recorded_total_bytes": volume.total_bytes,
            }
            for volume in volumes.values()
        ],
        "backlog": {
            "available_sources": len(backlog),
            "oldest_source_start": backlog[0].source_start.isoformat() if backlog else None,
            "queued_jobs": sum(job.state == "queued" for job in session.scalars(select(ArchiveJob)).all()),
        },
        "findings": [asdict(item) for item in findings],
    }

    if include_file_orphans:
        orphan_count = 0
        for volume_id, volume in volumes.items():
            root = Path(volume.mount_path_runtime) / "recordings"
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(Path(volume.mount_path_runtime)).as_posix()
                if ".quarantine" in path.parts:
                    findings.append(ArchiveFinding("QUARANTINED_FILE_PRESENT", "warning", volume_id, relative))
                elif path.name.endswith(".part"):
                    findings.append(ArchiveFinding("PART_FILE_PRESENT", "warning", volume_id, relative))
                elif path.suffix.lower() == ".mp4" and relative not in known_files[volume_id]:
                    findings.append(ArchiveFinding("FILE_WITHOUT_DB_ASSET", "warning", volume_id, relative))
                    orphan_count += 1
        result["file_orphan_scan"] = {"performed": True, "unregistered_mp4_count": orphan_count}
        result["findings"] = [asdict(item) for item in findings]
    else:
        result["file_orphan_scan"] = {"performed": False, "reason": "requires explicit --include-files"}
    return result
