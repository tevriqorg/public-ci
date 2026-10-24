import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from transfer_hub.api import create_app
from transfer_hub.archive_utils import ArchiveError, extract_archive
from transfer_hub.config import Settings


def _app(tmp_path: Path, *, start_worker: bool = False):
    token = tmp_path / "token"
    token.write_text("test")
    return create_app(Settings(tmp_path, "127.0.0.1", 0, token, tmp_path / "secrets", "http://127.0.0.1:9", tmp_path / "none"), start_worker=start_worker)


def test_clear_history_only_deletes_terminal_jobs_after_artifact_cleanup(tmp_path: Path):
    app = _app(tmp_path)
    service = app.state.service
    headers = {"Authorization": "Bearer test"}
    terminal = service.create_url_job({"source": "https://example.test/file", "source_type": "url"})
    ready = service.create_url_job({"source": "https://example.test/ready", "source_type": "url"})
    active = service.create_url_job({"source": "https://example.test/active", "source_type": "url"})
    app.state.store.update(terminal["id"], state="FAILED")
    app.state.store.update(ready["id"], state="READY")

    upload = tmp_path / "uploads" / "input.bin"
    upload.parent.mkdir(exist_ok=True, parents=True)
    upload.write_bytes(b"local input")
    local = service.create_local_job(upload, "input.bin")
    app.state.store.update(local["id"], state="CANCELLED")
    work = tmp_path / "jobs" / local["id"]
    work.mkdir(parents=True)
    (work / "artifact.bin").write_bytes(b"artifact")

    with TestClient(app) as client:
        protected = client.delete(f"/api/v1/jobs/{ready['id']}/history", headers=headers)
        assert protected.status_code == 409
        one = client.delete(f"/api/v1/jobs/{terminal['id']}/history", headers=headers)
        assert one.status_code == 200 and one.json()["cleared"]
        all_result = client.delete("/api/v1/jobs/history", headers=headers)
        assert all_result.status_code == 200
        assert all_result.json()["deleted"] == 1

    assert app.state.store.get_job(terminal["id"]) is None
    assert app.state.store.get_job(local["id"]) is None
    assert app.state.store.get_job(ready["id"])["state"] == "READY"
    assert app.state.store.get_job(active["id"])["state"] == "QUEUED"
    assert not upload.exists() and not work.exists()


def test_legacy_mobile_request_is_normalized_to_unified_secure_archive(tmp_path: Path, monkeypatch):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("folder/phone-ready.txt", "hello")
    app = _app(tmp_path)
    headers = {"Authorization": "Bearer test", "X-Filename": "input.zip", "X-Archive-Password": "secret-password"}
    captured = {}

    def fake_pack(source_path, output, password, volume_size, cancel):
        captured["source_name"] = source_path.name
        captured["password"] = password
        output.write_bytes(b"archive")
        return [output]

    monkeypatch.setattr("transfer_hub.service.pack_zip_aes", fake_pack)
    monkeypatch.setattr("transfer_hub.service.verify_7z_password", lambda *_args, **_kwargs: None)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/uploads?delivery_profile=iphone&pack=false&encrypt=false&delivery_backend=none&ack_policy=none",
            headers=headers,
            content=archive.getvalue(),
        )
        assert response.status_code == 202
        created = response.json()
        assert created["options"]["delivery_profile"] == "unified"
        assert created["options"]["pack"] is True
        assert created["options"]["encrypt"] is True
        assert created["options"]["pack_format"] == "zip-aes256"
        assert created["archive_password"] == "secret-password"
        app.state.service._execute(app.state.store.get_job(created["id"]))
        result = client.get(f"/api/v1/jobs/{created['id']}", headers={"Authorization": "Bearer test"}).json()
        assert result["state"] == "DONE"
        assert len(result["result"]["files"]) == 1
        assert result["result"]["files"][0].endswith(".zip")
        assert captured["password"] == "secret-password"
        events = client.get(f"/api/v1/jobs/{created['id']}/events", headers={"Authorization": "Bearer test"}).json()
        assert not any(event["type"] == "job.mobile_ready" for event in events)

def test_legacy_android_request_is_also_normalized_to_unified_secure_archive(tmp_path: Path, monkeypatch):
    apk = io.BytesIO()
    with zipfile.ZipFile(apk, "w") as zf:
        zf.writestr("AndroidManifest.xml", "manifest")
        zf.writestr("classes.dex", "dex")
    app = _app(tmp_path)
    headers = {"Authorization": "Bearer test", "X-Filename": "client.apk"}

    def fake_pack(source_path, output, password, volume_size, cancel):
        output.write_bytes(b"archive")
        return [output]

    monkeypatch.setattr("transfer_hub.service.pack_zip_aes", fake_pack)
    monkeypatch.setattr("transfer_hub.service.verify_7z_password", lambda *_args, **_kwargs: None)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/uploads?delivery_profile=android&delivery_backend=none&ack_policy=none",
            headers=headers,
            content=apk.getvalue(),
        )
        assert response.status_code == 202
        created = response.json()
        assert created["options"]["delivery_profile"] == "unified"
        assert created["options"]["pack"] is True
        assert created["options"]["encrypt"] is True
        app.state.service._execute(app.state.store.get_job(created["id"]))
        result = client.get(f"/api/v1/jobs/{created['id']}", headers={"Authorization": "Bearer test"}).json()
        assert result["state"] == "DONE"
        assert result["result"]["files"][0].endswith(".zip")
        events = client.get(f"/api/v1/jobs/{created['id']}/events", headers={"Authorization": "Bearer test"}).json()
        assert not any(event["type"] == "job.mobile_ready" for event in events)

def test_existing_legacy_mobile_job_keeps_original_plaintext_semantics(tmp_path: Path):
    root = tmp_path / "local-files"
    root.mkdir(parents=True)
    source = root / "legacy-client.apk"
    source.write_bytes(b"legacy-apk")
    app = _app(tmp_path)
    service = app.state.service
    job = service.create_local_job(source, source.name, {
        "delivery_backend": "none", "ack_policy": "none", "local_origin": "vps", "local_display_path": source.name,
    })
    legacy_options = dict(job["options"])
    legacy_options.update({"delivery_profile": "android", "pack": False, "encrypt": False, "pack_format": "7z"})
    app.state.store.update(job["id"], options=legacy_options, secret={})
    service._execute(app.state.store.get_job(job["id"]))
    result = app.state.store.get_job(job["id"])
    assert result["state"] == "DONE"
    assert result["options"]["delivery_profile"] == "android"
    assert result["options"]["pack"] is False
    assert result["options"]["encrypt"] is False
    assert result["result"]["files"][0].endswith(".apk")
    events = app.state.store.recent_events(job["id"], limit=100)
    assert any(event["type"] == "job.mobile_ready" for event in events)
    assert source.exists()


def test_zip_extraction_rejects_traversal(tmp_path: Path):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../escape.txt", "bad")
    with pytest.raises(ArchiveError):
        extract_archive(archive, tmp_path / "extract")
    assert not (tmp_path.parent / "escape.txt").exists()


def test_desktop_archive_uses_neutral_outer_name_but_preserves_inner_names(tmp_path: Path, monkeypatch):
    source = tmp_path / "private-family-video.mp4"
    source.write_bytes(b"private")
    app = _app(tmp_path)
    service = app.state.service
    job = service.create_local_job(source, source.name, {
        "delivery_backend": "none",
        "ack_policy": "none",
        "delivery_profile": "desktop",
    })
    captured = {}

    def fake_pack(source_path, output, password, volume_size, cancel):
        captured["source_name"] = source_path.name
        captured["output_name"] = output.name
        output.write_bytes(b"archive")
        return [output]

    monkeypatch.setattr("transfer_hub.service.pack_zip_aes", fake_pack)
    monkeypatch.setattr("transfer_hub.service.verify_7z_password", lambda *_args, **_kwargs: None)
    service._execute(app.state.store.get_job(job["id"]))
    result = app.state.store.get_job(job["id"])
    assert captured["source_name"] == "private-family-video.mp4"
    assert captured["output_name"].endswith("-x64.zip")
    assert "private-family-video" not in captured["output_name"]
    assert captured["output_name"].split("-v", 1)[0] in {
        "runtime-components", "sdk-resources", "container-base", "system-tools", "offline-resources"
    }
    assert result["options"]["public_names"] == [captured["output_name"]]
    assert result["options"]["public_names"][0].count("-x64") == 1
    assert result["state"] == "DONE"


def test_new_jobs_ignore_legacy_unpacked_flags_and_use_secure_neutral_archive(tmp_path: Path, monkeypatch):
    root = tmp_path / "local-files"
    root.mkdir(parents=True)
    source = root / "private-child-video.mp4"
    source.write_bytes(b"private")
    app = _app(tmp_path)
    service = app.state.service
    captured = {}

    def fake_pack(source_path, output, password, volume_size, cancel):
        captured["source_name"] = source_path.name
        captured["output_name"] = output.name
        output.write_bytes(b"archive")
        return [output]

    monkeypatch.setattr("transfer_hub.service.pack_zip_aes", fake_pack)
    monkeypatch.setattr("transfer_hub.service.verify_7z_password", lambda *_args, **_kwargs: None)
    job = service.create_local_job(source, source.name, {
        "delivery_backend": "none",
        "ack_policy": "none",
        "delivery_profile": "iphone",
        "pack": False,
        "encrypt": False,
        "local_origin": "vps",
        "local_display_path": source.name,
    })
    assert job["options"]["delivery_profile"] == "unified"
    assert job["options"]["pack"] is True
    assert job["options"]["encrypt"] is True
    service._execute(app.state.store.get_job(job["id"]))
    result = app.state.store.get_job(job["id"])
    delivered = Path(result["artifact_path"])
    assert result["state"] == "DONE"
    assert delivered.name.endswith(".zip")
    assert "private-child-video" not in delivered.name
    assert captured["source_name"] == "private-child-video.mp4"
    assert source.exists()

def test_qq_delivery_uploads_secure_archive_and_preserves_vps_source(tmp_path: Path, monkeypatch):
    root = tmp_path / "local-files"
    root.mkdir(parents=True)
    source = root / "private-family-video.mp4"
    source.write_bytes(b"private")
    app = _app(tmp_path)
    service = app.state.service
    job = service.create_local_job(source, source.name, {
        "delivery_backend": "qq_flash",
        "delivery_profile": "iphone",
        "ack_policy": "required",
        "local_origin": "vps",
        "local_display_path": source.name,
    })
    captured = {}

    def fake_pack(source_path, output, password, volume_size, cancel):
        output.write_bytes(b"archive")
        return [output]

    def fake_upload(path, title=None, filename=None, **_kwargs):
        captured["path"] = path
        captured["title"] = title
        captured["filename"] = filename
        return {"ok": True, "file_set_id": "fake", "share_link": "https://qfile.qq.com/q/fake"}

    monkeypatch.setattr("transfer_hub.service.pack_zip_aes", fake_pack)
    monkeypatch.setattr("transfer_hub.service.verify_7z_password", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service.flash, "upload", fake_upload)
    service._execute(app.state.store.get_job(job["id"]))
    result = app.state.store.get_job(job["id"])
    public_name = result["options"]["public_names"][0]
    assert result["state"] == "READY"
    assert result["options"]["delivery_profile"] == "unified"
    assert result["options"]["encrypt"] is True
    assert captured["path"] != source
    assert Path(captured["path"]).suffix == ".zip"
    assert captured["filename"] == public_name
    assert captured["title"] == public_name
    assert "private-family-video" not in public_name
    assert result["result"]["items"][0]["filename"] == public_name
    assert source.exists()
    service.ack(job["id"])
    assert source.exists(), "ACK must never delete a VPS local source file"
