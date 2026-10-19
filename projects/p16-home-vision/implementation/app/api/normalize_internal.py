from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_session
from app.domain.media_access import resolve_media_path
from app.domain.normalize_queue import NORMALIZATION_WORKER_PROTOCOL, claim_next_normalization, release_normalization_claim, renew_normalization_claim
from app.models import LocalAsset, SourceRecording, StorageVolume
from scripts.normalize_existing_assets import _register_normalized, _validate_output


class ClaimRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    release_revision: str = Field(min_length=40, max_length=40)
    protocol_version: str = Field(min_length=1, max_length=64)


class LeaseRequest(BaseModel):
    asset_id: str
    worker_id: str = Field(min_length=1, max_length=128)
    lease_token: str = Field(min_length=1, max_length=64)
    phase: str = Field(default="processing", min_length=1, max_length=32)
    error_code: str = Field(default="NORMALIZATION_FAILED", pattern=r"^[A-Z0-9_]+$", max_length=128)


router = APIRouter(prefix="/internal/normalize", tags=["normalize-internal"])


def _claim_payload(claim) -> dict[str, Any]:
    asset, source, volume = claim.asset, claim.source, claim.volume
    return {
        "asset_id": asset.id,
        "source_recording_id": source.id,
        "source_relative_path": asset.relative_path,
        "output_relative_path": f"recordings/normalized/{asset.id}.mp4",
        "source_audio_codec": asset.audio_codec,
        "duration_ms": asset.media_duration_ms or source.duration_ms,
        "source_size_bytes": asset.size_bytes,
        "lease_token": claim.token,
        "lease_expires_at": claim.lease_expires_at,
        "storage_volume_id": volume.id,
    }


@router.post("/claim")
def claim(request: ClaimRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
    if (
        not re.fullmatch(r"[0-9a-f]{40}", request.release_revision)
        or request.release_revision != os.environ.get("P16_RELEASE_REVISION")
        or request.protocol_version != NORMALIZATION_WORKER_PROTOCOL
    ):
        raise HTTPException(status_code=409, detail="NORMALIZATION_PROTOCOL_MISMATCH")
    result = claim_next_normalization(session, request.worker_id)
    return {"claim": _claim_payload(result) if result else None}


@router.post("/heartbeat")
def heartbeat(request: LeaseRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
    if not renew_normalization_claim(session, request.asset_id, request.worker_id, request.lease_token, request.phase):
        raise HTTPException(status_code=409, detail="NORMALIZATION_LEASE_LOST")
    return {"ok": True}


@router.post("/fail")
def fail(request: LeaseRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
    if not release_normalization_claim(
        session, request.asset_id, request.worker_id, request.lease_token, "failed", request.error_code
    ):
        raise HTTPException(status_code=409, detail="NORMALIZATION_LEASE_LOST")
    return {"ok": True}


@router.post("/complete")
def complete(request: LeaseRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
    asset = session.get(LocalAsset, request.asset_id)
    now = datetime.now(timezone.utc)
    lease_expires_at = asset.normalization_lease_expires_at if asset else None
    if lease_expires_at is not None and lease_expires_at.tzinfo is None:
        lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)
    if (
        asset is None
        or asset.normalization_worker_id != request.worker_id
        or asset.normalization_lease_token != request.lease_token
        or lease_expires_at is None
        or lease_expires_at <= now
    ):
        raise HTTPException(status_code=409, detail="NORMALIZATION_LEASE_LOST")
    source = session.get(SourceRecording, asset.source_recording_id)
    volume = session.get(StorageVolume, asset.storage_volume_id)
    if source is None or volume is None:
        raise HTTPException(status_code=422, detail="NORMALIZATION_METADATA_MISSING")
    final_path = resolve_media_path(Path(volume.mount_path_runtime), f"recordings/normalized/{asset.id}.mp4")
    if not final_path.is_file():
        raise HTTPException(status_code=422, detail="NORMALIZED_FILE_MISSING")
    try:
        container, output_audio_codec, output_duration_ms = _validate_output(
            final_path,
            expected_duration_ms=asset.media_duration_ms or source.duration_ms,
            source_audio_codec=asset.audio_codec,
            full_decode=False,
        )
        _register_normalized(asset, volume, final_path, output_audio_codec, output_duration_ms)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"NORMALIZED_VALIDATION_FAILED:{type(exc).__name__}") from exc
    asset.normalization_worker_id = None
    asset.normalization_lease_token = None
    asset.normalization_lease_expires_at = None
    asset.normalization_phase = "completed"
    asset.normalization_error_code = None
    session.commit()
    return {"ok": True, "asset_id": asset.id, "container": container, "audio_codec": output_audio_codec}
