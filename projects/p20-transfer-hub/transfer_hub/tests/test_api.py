import time
from pathlib import Path

from fastapi.testclient import TestClient

from transfer_hub.api import create_app, _source_from_input
from transfer_hub.config import Settings


def test_local_upload_full_job(tmp_path: Path):
    import shutil
    import pytest
    if not (shutil.which("7z") or shutil.which("7zz")):
        pytest.skip("7z is required because all P20 transfers are encrypted ZIP archives")
    token_file = tmp_path / "api.token"
    token_file.write_text("test-token")
    settings = Settings(
        data_dir=tmp_path,
        bind="127.0.0.1",
        port=0,
        api_token_file=token_file,
        secrets_dir=tmp_path / "secrets",
        qqxfer_url="http://127.0.0.1:9",
        qqxfer_token_file=tmp_path / "missing-qqxfer-token",
        worker_poll_seconds=0.01,
    )
    app = create_app(settings=settings, start_worker=True)
    headers = {"Authorization": "Bearer test-token", "X-Filename": "hello.txt"}

    with TestClient(app) as client:
        r = client.post(
            "/api/v1/uploads?delivery_backend=none&ack_policy=none",
            headers=headers,
            content=b"hello transfer hub",
        )
        assert r.status_code == 202
        job_id = r.json()["id"]

        deadline = time.time() + 3
        state = None
        while time.time() < deadline:
            r = client.get(f"/api/v1/jobs/{job_id}", headers={"Authorization": "Bearer test-token"})
            assert r.status_code == 200
            state = r.json()["state"]
            if state == "DONE":
                break
            time.sleep(0.03)

        assert state == "DONE"
        assert r.json()["sha256"]


def test_web_access_has_no_browser_session_login(tmp_path: Path):
    token_file = tmp_path / "api.token"
    token_file.write_text("test-token")
    settings = Settings(
        data_dir=tmp_path,
        bind="127.0.0.1",
        port=0,
        api_token_file=token_file,
        secrets_dir=tmp_path / "secrets",
        qqxfer_url="http://127.0.0.1:9",
        qqxfer_token_file=tmp_path / "missing-qqxfer-token",
    )
    app = create_app(settings=settings, start_worker=False)

    with TestClient(app) as client:
        assert client.get("/api/v1/jobs").status_code == 401
        assert client.get("/api/v1/session").status_code == 404
        assert client.post("/api/v1/session", headers={"Authorization": "Bearer test-token"}).status_code == 404
        assert client.get("/api/v1/jobs", headers={"Authorization": "Bearer test-token"}).status_code == 200


def test_internal_healthcheck_does_not_refresh_idle_marker(tmp_path: Path):
    token_file = tmp_path / "api.token"
    token_file.write_text("test-token")
    settings = Settings(
        data_dir=tmp_path, bind="127.0.0.1", port=0, api_token_file=token_file,
        secrets_dir=tmp_path / "secrets", qqxfer_url="http://127.0.0.1:9",
        qqxfer_token_file=tmp_path / "missing-qqxfer-token",
    )
    app = create_app(settings=settings, start_worker=False)
    marker = tmp_path / "hub-last-request"
    with TestClient(app) as client:
        assert client.get("/health", headers={"X-P20-Healthcheck": "1"}).status_code == 200
        assert not marker.exists()
        assert client.get("/health").status_code == 200
        assert marker.exists()

def test_job_events_endpoint(tmp_path: Path):
    token_file = tmp_path / "api.token"
    token_file.write_text("test-token")
    settings = Settings(
        data_dir=tmp_path,
        bind="127.0.0.1",
        port=0,
        api_token_file=token_file,
        secrets_dir=tmp_path / "secrets",
        qqxfer_url="http://127.0.0.1:9",
        qqxfer_token_file=tmp_path / "missing-qqxfer-token",
    )
    app = create_app(settings=settings, start_worker=False)
    headers = {"Authorization": "Bearer test-token"}

    job = app.state.service.create_url_job({
        "source": "https://example.com/a.bin",
        "source_type": "url",
        "delivery_backend": "none",
    })
    with TestClient(app) as client:
        response = client.get(f"/api/v1/jobs/{job['id']}/events", headers=headers)
        assert response.status_code == 200
        events = response.json()
        assert events
        assert events[0]["type"] == "job.created"


def test_local_upload_manual_archive_password(tmp_path: Path):
    token_file = tmp_path / "api.token"
    token_file.write_text("test-token")
    settings = Settings(
        data_dir=tmp_path,
        bind="127.0.0.1",
        port=0,
        api_token_file=token_file,
        secrets_dir=tmp_path / "secrets",
        qqxfer_url="http://127.0.0.1:9",
        qqxfer_token_file=tmp_path / "missing-qqxfer-token",
    )
    app = create_app(settings=settings, start_worker=False)
    headers = {
        "Authorization": "Bearer test-token",
        "X-Filename": "manual.txt",
        "X-Archive-Password": "manual-secret-123",
    }

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/uploads?delivery_backend=none&encrypt=true",
            headers=headers,
            content=b"manual password test",
        )
        assert response.status_code == 202
        body = response.json()
        assert body["archive_password"] == "manual-secret-123"



def test_vps_local_file_picker_supports_extra_allowlisted_root(tmp_path: Path, monkeypatch):
    token_file = tmp_path / "api.token"
    token_file.write_text("test-token")
    extra = tmp_path / "jable-downloader-data"
    downloads = extra / "downloads"
    downloads.mkdir(parents=True)
    media = downloads / "sample.mp4"
    media.write_bytes(b"video")
    monkeypatch.setenv("P20_LOCAL_FILE_ROOTS", str(extra))
    settings = Settings(
        data_dir=tmp_path / "p20", bind="127.0.0.1", port=0, api_token_file=token_file,
        secrets_dir=tmp_path / "p20" / "secrets", qqxfer_url="http://127.0.0.1:9",
        qqxfer_token_file=tmp_path / "missing-qqxfer-token",
    )
    app = create_app(settings=settings, start_worker=False)
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        root = client.get("/api/v1/local-files", headers=headers).json()
        assert {x["name"] for x in root["entries"]} == {"local-files", "jable-downloader-data"}
        listing = client.get("/api/v1/local-files?path=jable-downloader-data/downloads", headers=headers)
        assert listing.status_code == 200
        assert listing.json()["entries"][0]["path"] == "jable-downloader-data/downloads/sample.mp4"
        created = client.post(
            "/api/v1/local-files/jobs", headers=headers,
            json={"path": "jable-downloader-data/downloads/sample.mp4", "delivery_backend": "none", "delivery_profile": "iphone"},
        )
        assert created.status_code == 202
        body = created.json()
        assert body["options"]["local_display_path"] == "jable-downloader-data/downloads/sample.mp4"
        assert "local_root" not in body["options"]
        assert client.post(
            "/api/v1/local-files/jobs", headers=headers,
            json={"path": "jable-downloader-data/../outside.bin"},
        ).status_code == 400

def test_pause_rejects_non_active_aria2(tmp_path: Path):
    token_file = tmp_path / "api.token"
    token_file.write_text("test-token")
    settings = Settings(
        data_dir=tmp_path,
        bind="127.0.0.1",
        port=0,
        api_token_file=token_file,
        secrets_dir=tmp_path / "secrets",
        qqxfer_url="http://127.0.0.1:9",
        qqxfer_token_file=tmp_path / "missing-qqxfer-token",
    )
    app = create_app(settings=settings, start_worker=False)
    headers = {"Authorization": "Bearer test-token"}
    job = app.state.service.create_url_job({
        "source": "https://example.com/a.bin",
        "source_type": "url",
        "delivery_backend": "none",
    })
    with TestClient(app) as client:
        response = client.post(f"/api/v1/jobs/{job['id']}/pause", headers=headers, json={})
        assert response.status_code == 409


def test_local_upload_accepts_mac_zip_compat(tmp_path: Path):
    token_file = tmp_path / "api.token"
    token_file.write_text("test-token")
    settings = Settings(
        data_dir=tmp_path, bind="127.0.0.1", port=0, api_token_file=token_file,
        secrets_dir=tmp_path / "secrets", qqxfer_url="http://127.0.0.1:9",
        qqxfer_token_file=tmp_path / "missing-qqxfer-token",
    )
    app = create_app(settings=settings, start_worker=False)
    headers = {"Authorization": "Bearer test-token", "X-Filename": "hello.txt"}
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/uploads?delivery_backend=none",
            headers=headers, content=b"hello",
        )
        assert response.status_code == 202
        body = response.json()
        assert body["options"]["pack_format"] == "zip-aes256"
        assert len(body["archive_password"]) == 32


def test_jobs_force_secure_packaging(tmp_path: Path):
    token_file = tmp_path / "api.token"
    token_file.write_text("test-token")
    settings = Settings(data_dir=tmp_path, bind="127.0.0.1", port=0, api_token_file=token_file,
                        secrets_dir=tmp_path / "secrets", qqxfer_url="http://127.0.0.1:9",
                        qqxfer_token_file=tmp_path / "missing-qqxfer-token")
    app = create_app(settings=settings, start_worker=False)
    job = app.state.service.create_url_job({
        "source": "https://example.com/a.bin", "source_type": "url",
        "pack": False, "encrypt": False, "pack_format": "7z", "delivery_backend": "none",
    })
    assert job["options"]["pack"] is True
    assert job["options"]["encrypt"] is True
    assert job["options"]["pack_format"] == "zip-aes256"
    assert len(job["secret"]["archive_password"]) == 32


def test_tailnet_client_does_not_need_browser_token(tmp_path: Path):
    token_file = tmp_path / "api.token"
    token_file.write_text("test-token")
    settings = Settings(
        data_dir=tmp_path, bind="127.0.0.1", port=0, api_token_file=token_file,
        secrets_dir=tmp_path / "secrets", qqxfer_url="http://127.0.0.1:9",
        qqxfer_token_file=tmp_path / "missing-qqxfer-token",
    )
    app = create_app(settings=settings, start_worker=False)
    with TestClient(app, client=("100.100.100.100", 50000)) as client:
        assert client.get("/api/v1/jobs").status_code == 200



def test_command_input_extracts_url_and_engine():
    source, engine = _source_from_input("N_m3u8DL-RE 'https://example.com/video/master.m3u8' --auto-select")
    assert source == "https://example.com/video/master.m3u8"
    assert engine == "nm3u8dl"


def test_vps_local_file_picker_is_rooted_and_creates_local_job(tmp_path: Path, monkeypatch):
    token_file = tmp_path / "api.token"
    token_file.write_text("test-token")
    settings = Settings(
        data_dir=tmp_path,
        bind="127.0.0.1",
        port=0,
        api_token_file=token_file,
        secrets_dir=tmp_path / "secrets",
        qqxfer_url="http://127.0.0.1:9",
        qqxfer_token_file=tmp_path / "missing-qqxfer-token",
    )
    monkeypatch.delenv("P20_LOCAL_FILE_ROOTS", raising=False)
    app = create_app(settings=settings, start_worker=False)
    root = tmp_path / "local-files"
    nested = root / "incoming"
    nested.mkdir(parents=True)
    selected = nested / "movie.bin"
    selected.write_bytes(b"hello")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"secret")
    try:
        (root / "escape-link").symlink_to(outside)
    except OSError:
        pass

    headers = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        listing = client.get("/api/v1/local-files", headers=headers)
        assert listing.status_code == 200
        names = {item["name"] for item in listing.json()["entries"]}
        assert "local-files" in names

        root_listing = client.get("/api/v1/local-files?path=local-files", headers=headers)
        assert root_listing.status_code == 200
        root_names = {item["name"] for item in root_listing.json()["entries"]}
        assert "incoming" in root_names
        assert "escape-link" not in root_names

        nested_listing = client.get("/api/v1/local-files?path=local-files/incoming", headers=headers)
        assert nested_listing.status_code == 200
        assert nested_listing.json()["entries"][0]["path"] == "local-files/incoming/movie.bin"

        created = client.post(
            "/api/v1/local-files/jobs",
            headers=headers,
            json={"path": "local-files/incoming/movie.bin", "delivery_backend": "none", "delivery_profile": "iphone"},
        )
        assert created.status_code == 202
        job = created.json()
        assert job["source_type"] == "local"
        assert job["filename"] == "movie.bin"
        assert job["options"]["local_origin"] == "vps"
        assert job["options"]["local_display_path"] == "local-files/incoming/movie.bin"
        assert job["options"]["delivery_profile"] == "unified"
        assert job["options"]["pack"] is True
        assert job["options"]["encrypt"] is True
        assert job["options"]["pack_format"] == "zip-aes256"
        assert job["public_name_preview"]
        assert job["public_name_preview"].endswith(".zip")
        assert "local_path" not in job["options"]

        assert client.post("/api/v1/local-files/jobs", headers=headers, json={"path": "../outside.bin"}).status_code == 400
        assert client.post("/api/v1/local-files/jobs", headers=headers, json={"path": str(outside)}).status_code == 400
