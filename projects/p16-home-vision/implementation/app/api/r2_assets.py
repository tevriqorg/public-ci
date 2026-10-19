from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(prefix="/api/r2", tags=["p16r2-poc"])


def _asset_root() -> Path:
    return Path(os.environ.get("HOME_VISION_R2_ASSET_ROOT", "/state/r2-assets"))


def _safe_asset_dir(asset_id: str) -> Path:
    if not asset_id or any(part in asset_id for part in ("/", "\\", "..")):
        raise HTTPException(status_code=400, detail="invalid asset id")
    return _asset_root() / asset_id


def _read_status(path: Path) -> dict[str, object] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None


def asset_projection(asset_id: str) -> dict[str, object]:
    """Expose R2 resource state without private filesystem paths."""
    status = _read_status(_safe_asset_dir(asset_id) / "status.json")
    if not isinstance(status, dict) or status.get("asset_id") != asset_id:
        return {}
    return {key: status[key] for key in ("state", "stages", "camera", "started_at", "raw_deleted", "error_code") if key in status}


def ready_web_media(asset_id: str) -> Path | None:
    projection = asset_projection(asset_id)
    stages = projection.get("stages")
    media = _safe_asset_dir(asset_id) / "web" / "video.mp4"
    if isinstance(stages, dict) and stages.get("web") == "done" and media.is_file() and media.stat().st_size > 0:
        return media
    return None


@router.get("/assets")
def list_r2_assets() -> dict[str, object]:
    root = _asset_root()
    if not root.exists():
        return {"items": []}

    items: list[dict[str, object]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        status = _read_status(child / "status.json")
        if status is not None:
            items.append(status)

    items.sort(
        key=lambda item: (
            str(item.get("started_at") or ""),
            str(item.get("asset_id") or ""),
        ),
        reverse=True,
    )
    return {"items": items}


@router.get("/assets/{asset_id}")
def get_r2_asset(asset_id: str) -> dict[str, object]:
    status = _read_status(_safe_asset_dir(asset_id) / "status.json")
    if status is None:
        raise HTTPException(status_code=404, detail="asset not found")
    return status


@router.get("/assets/{asset_id}/web")
def get_r2_web_media(asset_id: str) -> FileResponse:
    media = ready_web_media(asset_id)
    if media is None:
        raise HTTPException(status_code=404, detail="web media not ready")
    return FileResponse(media, media_type="video/mp4", filename=f"{asset_id}.mp4")
