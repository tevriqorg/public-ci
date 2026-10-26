from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.manifest import upsert_source_recording
from app.infra.nvr.hikvision import DiscoveredSegment
from app.models import LocalAsset, SourceRecording, StorageVolume


@dataclass(frozen=True)
class ReconciliationResult:
    source_ids: tuple[str, ...]
    discovered_count: int


@dataclass(frozen=True)
class StorageFinding:
    code: str
    volume_id: str
    relative_path: str
    asset_id: str | None = None


@dataclass(frozen=True)
class StorageReconciliationResult:
    findings: tuple[StorageFinding, ...]

    @property
    def clean(self) -> bool:
        return not self.findings


def reconcile_storage_files(session: Session) -> StorageReconciliationResult:
    """Report DB/file mismatches without mutating assets, media, or audit state."""
    volumes = {volume.id: volume for volume in session.scalars(select(StorageVolume))}
    assets = list(session.scalars(select(LocalAsset)))
    by_location: dict[tuple[str, str], list[LocalAsset]] = {}
    findings: list[StorageFinding] = []

    for asset in assets:
        by_location.setdefault((asset.storage_volume_id, asset.relative_path), []).append(asset)

    for volume_id, volume in sorted(volumes.items()):
        root = Path(volume.mount_path_runtime)
        if not volume.online:
            findings.append(StorageFinding("VOLUME_OFFLINE", volume_id, ""))
            continue
        if root.is_symlink():
            findings.append(StorageFinding("VOLUME_ROOT_SYMLINK", volume_id, ""))
            continue
        try:
            root.stat()
        except FileNotFoundError:
            findings.append(StorageFinding("VOLUME_ROOT_MISSING", volume_id, ""))
            continue
        except OSError:
            findings.append(StorageFinding("VOLUME_ROOT_UNREADABLE", volume_id, ""))
            continue
        if not root.is_dir():
            findings.append(StorageFinding("VOLUME_ROOT_NOT_DIRECTORY", volume_id, ""))
            continue

        resolved_root = root.resolve(strict=False)
        volume_assets = [asset for asset in assets if asset.storage_volume_id == volume_id]
        for asset in volume_assets:
            relative = Path(asset.relative_path)
            if (not asset.relative_path or not relative.parts or relative.is_absolute()
                    or ".." in relative.parts):
                findings.append(StorageFinding(
                    "ASSET_PATH_UNSAFE", volume_id, asset.relative_path, asset.id
                ))
                continue
            candidate = root / relative
            cursor = root
            unsafe = False
            for part in relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    unsafe = True
                    break
            if unsafe or not candidate.resolve(strict=False).is_relative_to(resolved_root):
                findings.append(StorageFinding(
                    "ASSET_PATH_UNSAFE", volume_id, asset.relative_path, asset.id
                ))
                continue
            try:
                candidate.stat()
                exists = True
            except FileNotFoundError:
                exists = False
            except OSError:
                findings.append(StorageFinding(
                    "ASSET_PATH_UNREADABLE", volume_id, asset.relative_path, asset.id
                ))
                continue

            if asset.lifecycle_state == "present" and asset.verification_state == "verified" and not exists:
                findings.append(StorageFinding(
                    "DB_ONLY_ASSET", volume_id, asset.relative_path, asset.id
                ))
            elif asset.lifecycle_state == "deleted" and exists:
                findings.append(StorageFinding(
                    "DELETED_FILE_PRESENT", volume_id, asset.relative_path, asset.id
                ))
            elif asset.lifecycle_state == "missing" and exists:
                findings.append(StorageFinding(
                    "FILE_PRESENT_FOR_MISSING_ASSET", volume_id, asset.relative_path, asset.id
                ))

        recordings = root / "recordings"
        if recordings.is_symlink():
            findings.append(StorageFinding("RECORDINGS_ROOT_SYMLINK", volume_id, "recordings"))
            continue
        if not recordings.is_dir():
            continue
        for path in recordings.rglob("*.mp4"):
            if path.is_symlink():
                findings.append(StorageFinding(
                    "MEDIA_FILE_SYMLINK", volume_id, path.relative_to(root).as_posix()
                ))
                continue
            if not path.is_file():
                continue
            relative_path = path.relative_to(root).as_posix()
            if not by_location.get((volume_id, relative_path)):
                findings.append(StorageFinding("FILE_ONLY_MEDIA", volume_id, relative_path))

    for asset in assets:
        if asset.storage_volume_id not in volumes:
            findings.append(StorageFinding(
                "UNKNOWN_STORAGE_VOLUME", asset.storage_volume_id,
                asset.relative_path, asset.id,
            ))

    return StorageReconciliationResult(tuple(sorted(
        findings, key=lambda item: (item.code, item.volume_id, item.relative_path, item.asset_id or "")
    )))


def reconcile_segments(
    session: Session,
    *,
    nvr_id: str,
    segments: Iterable[DiscoveredSegment],
    scan_id: str | None,
) -> ReconciliationResult:
    ids: list[str] = []
    count = 0
    for segment in segments:
        recording = upsert_source_recording(
            session,
            nvr_id=nvr_id,
            track_id=segment.track_id,
            source_start=segment.source_start,
            source_end=segment.source_end,
            duration_ms=segment.duration_ms,
            locator_fingerprint=segment.locator_fingerprint,
            source_size_if_known=segment.source_size_if_known,
            scan_id=scan_id,
        )
        recording.content_type = segment.content_type
        recording.recording_type = segment.recording_type
        ids.append(recording.id)
        count += 1
    session.flush()
    return ReconciliationResult(tuple(ids), count)


def scan_state_advances_expiry(state: str) -> bool:
    return state == "success"


def should_mark_expired(
    recording: SourceRecording,
    *,
    successful_covering_misses: int,
    current_write_edge: datetime,
    guard: timedelta = timedelta(hours=1),
) -> bool:
    """Conservative R6 rule: two successful misses and clearly behind write edge."""
    if successful_covering_misses < 2:
        return False
    source_end = recording.source_end
    if source_end.tzinfo is None:
        source_end = source_end.replace(tzinfo=current_write_edge.tzinfo)
    return source_end <= current_write_edge - guard
