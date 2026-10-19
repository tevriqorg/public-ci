from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.r2_assets import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_list_assets_reads_status_files(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME_VISION_R2_ASSET_ROOT", str(tmp_path))
    asset = tmp_path / "a1"
    asset.mkdir()
    (asset / "status.json").write_text(
        json.dumps(
            {
                "asset_id": "a1",
                "started_at": "2026-10-15T09:00:00+08:00",
                "state": "derived",
                "raw_deleted": False,
                "stages": {"raw": "done", "web": "done"},
            }
        ),
        encoding="utf-8",
    )

    response = _client().get("/api/r2/assets")
    assert response.status_code == 200
    assert response.json()["items"][0]["asset_id"] == "a1"


def test_invalid_asset_id_cannot_escape_root(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME_VISION_R2_ASSET_ROOT", str(tmp_path))
    response = _client().get("/api/r2/assets/../secret")
    assert response.status_code in (400, 404)


def test_web_media_is_not_available_until_promoted(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME_VISION_R2_ASSET_ROOT", str(tmp_path))
    asset = tmp_path / "a1"
    asset.mkdir()
    response = _client().get("/api/r2/assets/a1/web")
    assert response.status_code == 404
