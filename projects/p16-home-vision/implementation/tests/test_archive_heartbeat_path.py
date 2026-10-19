import json
from datetime import datetime, timedelta, timezone

from app.config import Settings, get_settings
from app.api import archive_internal
from scripts import archive_worker, check_archive_worker_heartbeat


def test_default_heartbeat_path_preserves_container_contract():
    assert str(Settings().archive_heartbeat_path) == "/state/archive-worker-heartbeat.json"


def test_worker_api_and_health_check_share_configured_local_path(monkeypatch, tmp_path):
    path = tmp_path / "archive-heartbeat.json"
    monkeypatch.setenv("HOME_VISION_ARCHIVE_HEARTBEAT_PATH", str(path))
    get_settings.cache_clear()
    try:
        monkeypatch.setattr(archive_worker, "HEARTBEAT_PATH", get_settings().archive_heartbeat_path)
        archive_worker._touch_heartbeat("polling", force=True)
        assert path.is_file()
        assert archive_internal._worker_heartbeat()["status"] == "healthy"
        assert check_archive_worker_heartbeat.main() == 0

        path.write_text(json.dumps({"heartbeat_at": (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(), "phase": "polling"}))
        assert archive_internal._worker_heartbeat()["status"] == "stale"
        assert check_archive_worker_heartbeat.main() == 1

        path.unlink()
        assert archive_internal._worker_heartbeat()["status"] == "not_seen"
        assert check_archive_worker_heartbeat.main() == 1
    finally:
        get_settings.cache_clear()
