from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.domain.media_access import resolve_media_path
from app.models import LocalAsset, SourceRecording, StorageVolume


LEASE_SECONDS = 15 * 60
MAX_ATTEMPTS = 3
NORMALIZATION_START = datetime(2026, 9, 1, tzinfo=timezone.utc)
NORMALIZATION_WORKERS = {f"mba-normalize-{slot}" for slot in range(1, 4)}
NORMALIZATION_WORKER_PROTOCOL = "p16-normalize-mba-v1"


@dataclass(frozen=True)
class NormalizationClaim:
    asset: LocalAsset
    source: SourceRecording
    volume: StorageVolume
    token: str
    lease_expires_at: datetime


def claim_next_normalization(session: Session, worker_id: str, now: datetime | None = None) -> NormalizationClaim | None:
    if worker_id not in NORMALIZATION_WORKERS:
        return None
    now = now or datetime.now(timezone.utc)
    protected_from_legacy = LocalAsset.source_recording_id.in_(
        select(SourceRecording.id).where(SourceRecording.source_start >= NORMALIZATION_START)
    )
    session.execute(
        update(LocalAsset)
        .where(
            protected_from_legacy,
            LocalAsset.normalization_lease_expires_at.is_not(None),
            LocalAsset.normalization_lease_expires_at <= now,
            LocalAsset.normalization_attempt_count >= MAX_ATTEMPTS,
        )
        .values(
            normalization_worker_id=None,
            normalization_lease_token=None,
            normalization_lease_expires_at=None,
            normalization_phase="failed",
        )
        .execution_options(synchronize_session=False)
    )
    session.execute(
        update(LocalAsset)
        .where(
            protected_from_legacy,
            LocalAsset.normalization_lease_expires_at.is_not(None),
            LocalAsset.normalization_lease_expires_at <= now,
            LocalAsset.normalization_attempt_count < MAX_ATTEMPTS,
        )
        .values(
            normalization_worker_id=None,
            normalization_lease_token=None,
            normalization_lease_expires_at=None,
            normalization_phase=None,
        )
        .execution_options(synchronize_session=False)
    )
    session.commit()
    rows = session.execute(
        select(LocalAsset, SourceRecording, StorageVolume)
        .join(SourceRecording, SourceRecording.id == LocalAsset.source_recording_id)
        .join(StorageVolume, StorageVolume.id == LocalAsset.storage_volume_id)
        .where(
            LocalAsset.asset_role == "source_archive",
            LocalAsset.verification_state == "verified",
            LocalAsset.lifecycle_state == "present",
            StorageVolume.id == "archive-a",
            SourceRecording.source_start >= NORMALIZATION_START,
            or_(
                LocalAsset.normalization_lease_expires_at.is_(None),
                LocalAsset.normalization_lease_expires_at <= now,
            ),
        )
        .order_by(SourceRecording.source_start, LocalAsset.id)
    ).all()
    for asset, source, volume in rows:
        normalized = session.get(LocalAsset, f"normalized-{asset.id}")
        final_path = resolve_media_path(Path(volume.mount_path_runtime), f"recordings/normalized/{asset.id}.mp4")
        if normalized and normalized.verification_state == "verified" and normalized.lifecycle_state == "present":
            continue
        if asset.normalization_attempt_count >= MAX_ATTEMPTS:
            continue
        token = secrets.token_hex(24)
        expires = now + timedelta(seconds=LEASE_SECONDS)
        result = session.execute(
            update(LocalAsset)
            .where(
                LocalAsset.id == asset.id,
                or_(
                    LocalAsset.normalization_lease_expires_at.is_(None),
                    LocalAsset.normalization_lease_expires_at <= now,
                ),
            )
            .values(
                normalization_worker_id=worker_id,
                normalization_lease_token=token,
                normalization_lease_expires_at=expires,
                normalization_phase="claimed",
                normalization_error_code=None,
                normalization_attempt_count=LocalAsset.normalization_attempt_count + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            session.commit()
            asset.normalization_worker_id = worker_id
            asset.normalization_lease_token = token
            asset.normalization_lease_expires_at = expires
            asset.normalization_phase = "claimed"
            return NormalizationClaim(asset, source, volume, token, expires)
        session.rollback()
    return None


def renew_normalization_claim(
    session: Session,
    asset_id: str,
    worker_id: str,
    token: str,
    phase: str,
    now: datetime | None = None,
) -> bool:
    now = now or datetime.now(timezone.utc)
    expires = now + timedelta(seconds=LEASE_SECONDS)
    result = session.execute(
        update(LocalAsset)
        .where(
            LocalAsset.id == asset_id,
            LocalAsset.normalization_worker_id == worker_id,
            LocalAsset.normalization_lease_token == token,
            LocalAsset.normalization_lease_expires_at > now,
        )
        .values(normalization_phase=phase, normalization_lease_expires_at=expires)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return result.rowcount == 1


def release_normalization_claim(
    session: Session,
    asset_id: str,
    worker_id: str,
    token: str,
    phase: str | None = None,
    error_code: str | None = None,
) -> bool:
    result = session.execute(
        update(LocalAsset)
        .where(
            LocalAsset.id == asset_id,
            LocalAsset.normalization_worker_id == worker_id,
            LocalAsset.normalization_lease_token == token,
        )
        .values(
            normalization_worker_id=None,
            normalization_lease_token=None,
            normalization_lease_expires_at=None,
            normalization_phase=phase,
            normalization_error_code=error_code if phase == "failed" else None,
        )
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return result.rowcount == 1
