from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.domain.archive_pipeline import sha256_file
from app.models import AuditEvent, DeleteIntent, LocalAsset, Tombstone


class DeleteError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def resolve_asset_path(volume_root: Path, relative_path: str) -> Path:
    root = volume_root.resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise DeleteError("DELETE_PATH_ESCAPES_VOLUME") from exc
    return candidate


def create_delete_preview(
    session: Session,
    *,
    intent_id: str,
    asset: LocalAsset,
    requested_by: str,
    reason: str | None = None,
) -> DeleteIntent:
    intent = DeleteIntent(
        id=intent_id,
        local_asset_id=asset.id,
        source_recording_id=asset.source_recording_id,
        requested_by=requested_by,
        state="previewed",
        reason=reason,
        expected_relative_path=asset.relative_path,
        expected_sha256=asset.sha256,
        expected_size=asset.size_bytes,
    )
    session.add(intent)
    session.flush()
    return intent


def confirm_delete_intent(session: Session, intent: DeleteIntent) -> None:
    if intent.state != "previewed":
        raise DeleteError("DELETE_INTENT_NOT_PREVIEWED")
    intent.state = "confirmed"
    intent.confirmed_at = _utcnow()
    session.flush()


def _mark_failed(session: Session, intent: DeleteIntent, code: str) -> None:
    intent.state = "failed"
    intent.error_code = code
    session.flush()


def _validate_preview(asset: LocalAsset, intent: DeleteIntent, path: Path) -> None:
    if asset.relative_path != intent.expected_relative_path:
        raise DeleteError("DELETE_PREVIEW_STALE_PATH")
    if not path.exists() or not path.is_file():
        raise DeleteError("DELETE_FILE_MISSING_BEFORE_UNLINK")
    current_size = path.stat().st_size
    if intent.expected_size is not None and current_size != intent.expected_size:
        raise DeleteError("DELETE_PREVIEW_STALE_SIZE")
    if intent.expected_sha256 is not None and sha256_file(path) != intent.expected_sha256:
        raise DeleteError("DELETE_PREVIEW_STALE_HASH")


def _complete_delete_state(session: Session, *, intent: DeleteIntent, asset: LocalAsset) -> Tombstone:
    existing = session.get(Tombstone, f"tomb-{intent.id}")
    if existing is not None:
        intent.state = "completed"
        asset.lifecycle_state = "deleted"
        asset.deleted_at = existing.deleted_at
        session.flush()
        return existing

    now = _utcnow()
    tombstone = Tombstone(
        id=f"tomb-{intent.id}",
        source_recording_id=asset.source_recording_id,
        local_asset_id=asset.id,
        deleted_at=now,
        deleted_by=intent.requested_by,
        reason=intent.reason,
        previous_relative_path=intent.expected_relative_path,
        previous_size=intent.expected_size,
        previous_sha256=intent.expected_sha256,
        suppress_redownload=True,
    )
    session.add(tombstone)
    asset.lifecycle_state = "deleted"
    asset.deleted_at = now
    intent.state = "completed"
    intent.completed_at = now
    intent.error_code = None
    audit_id = f"audit-delete-{intent.id}"
    if session.get(AuditEvent, audit_id) is None:
        session.add(
            AuditEvent(
                id=audit_id,
                event_type="asset_physically_deleted",
                entity_type="local_asset",
                entity_id=asset.id,
                actor=intent.requested_by,
                payload_redacted=None,
            )
        )
    session.flush()
    return tombstone


def execute_confirmed_delete(
    session: Session,
    *,
    intent: DeleteIntent,
    asset: LocalAsset,
    volume_root: Path,
) -> Tombstone:
    if intent.state not in {"confirmed", "deleting"}:
        raise DeleteError("DELETE_NOT_CONFIRMED")
    path = resolve_asset_path(volume_root, intent.expected_relative_path)

    if intent.state == "confirmed":
        try:
            _validate_preview(asset, intent, path)
        except DeleteError as exc:
            _mark_failed(session, intent, exc.code)
            raise
        intent.state = "deleting"
        session.flush()

    if path.exists():
        try:
            if intent.expected_size is not None and path.stat().st_size != intent.expected_size:
                raise DeleteError("DELETE_RECOVERY_SIZE_MISMATCH")
            if intent.expected_sha256 is not None and sha256_file(path) != intent.expected_sha256:
                raise DeleteError("DELETE_RECOVERY_HASH_MISMATCH")
            path.unlink()
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except DeleteError as exc:
            _mark_failed(session, intent, exc.code)
            raise
        except OSError as exc:
            _mark_failed(session, intent, "DELETE_UNLINK_FAILED")
            raise DeleteError("DELETE_UNLINK_FAILED") from exc

    if path.exists():
        _mark_failed(session, intent, "DELETE_FILE_STILL_PRESENT")
        raise DeleteError("DELETE_FILE_STILL_PRESENT")

    return _complete_delete_state(session, intent=intent, asset=asset)
