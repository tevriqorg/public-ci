from fastapi.testclient import TestClient

from app.main import app


def test_health_endpoint() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["database"] == "ok"
    assert payload["normalize_worker_protocol"] == "p16-normalize-mba-v1"
    assert "p16_release_revision" in payload


def test_runtime_status_reports_real_container_counters() -> None:
    response = TestClient(app).get("/api/v1/runtime-status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["runtime"] == "container"
    assert set(payload["cpu"]) == {"load_1m", "logical_cores"}
    assert set(payload["memory"]) == {"used_bytes", "limit_bytes"}
    assert set(payload["network"]) == {"received_bytes", "transmitted_bytes"}
    assert payload["host"]["status"] == "not_collected"
