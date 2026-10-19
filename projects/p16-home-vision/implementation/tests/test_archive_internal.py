from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import archive_internal


def _settings(enabled: bool = True):
    return SimpleNamespace(archive_queue_enabled=enabled, archive_download_method="POST")


def test_archive_status_distinguishes_configured_from_ready(monkeypatch) -> None:
    monkeypatch.setattr(archive_internal, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        archive_internal,
        "_worker_heartbeat",
        lambda: {"status": "stale", "phase": "polling", "heartbeat_at": None, "age_seconds": 45.0},
    )

    payload = archive_internal.archive_status()

    assert payload["configured"] is True
    assert payload["enqueue_enabled"] is True
    assert payload["enqueue_ready"] is False
    assert "pause_queued" in payload["control_actions"]
    assert "cancel_waiting" in payload["control_actions"]
    assert "resume_paused" not in payload["control_actions"]
    assert "retry_failed" not in payload["control_actions"]


def test_enqueue_guard_rejects_missing_fresh_worker_heartbeat(monkeypatch) -> None:
    monkeypatch.setattr(archive_internal, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        archive_internal,
        "_worker_heartbeat",
        lambda: {"status": "not_seen", "phase": None, "heartbeat_at": None, "age_seconds": None},
    )

    with pytest.raises(HTTPException) as exc_info:
        archive_internal._require_enqueue_ready()

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "ARCHIVE_WORKER_NOT_SEEN"


def test_enqueue_guard_accepts_fresh_worker_heartbeat(monkeypatch) -> None:
    monkeypatch.setattr(archive_internal, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        archive_internal,
        "_worker_heartbeat",
        lambda: {"status": "healthy", "phase": "polling", "heartbeat_at": None, "age_seconds": 3.0},
    )

    archive_internal._require_enqueue_ready()
    payload = archive_internal.archive_status()

    assert payload["enqueue_ready"] is True
    assert "resume_paused" in payload["control_actions"]
    assert "retry_failed" in payload["control_actions"]


def test_queue_guard_rejects_unconfigured_runtime(monkeypatch) -> None:
    monkeypatch.setattr(archive_internal, "get_settings", lambda: _settings(enabled=False))

    with pytest.raises(HTTPException) as exc_info:
        archive_internal._require_queue_configured()

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "ARCHIVE_WORKER_NOT_CONFIGURED"
