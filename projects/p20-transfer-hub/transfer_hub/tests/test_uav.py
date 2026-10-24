import json
import sys
from pathlib import Path

import pytest

from transfer_hub import engines, uav
from transfer_hub.api import _source_from_input
from transfer_hub.api import create_app
from fastapi.testclient import TestClient
from transfer_hub.config import Settings
from transfer_hub.db import Store
from transfer_hub.resolver import resolve_source
from transfer_hub.service import TransferService


@pytest.mark.parametrize("source", ["FC2-PPV-1460748", "IPX-850", "300MIUM-1483", "https://jable.tv/videos/ipx-850/", "https://missav.ai/en/fc2-ppv-1460748", "https://supjav.com/12345.html"])
def test_site_task_skips_generic_discovery(source, monkeypatch):
    monkeypatch.setattr("transfer_hub.resolver._extract_manifest_from_page", lambda *_: pytest.fail("generic fetch"))
    assert resolve_source("auto", source, "video.mp4").engine_label == "uav"


@pytest.mark.parametrize("source", ["https://jable.tv.evil.test/videos/a/", "https://jable.tv@evil.test/videos/a/", "https://supjav.com:9000/123.html", "https://missav.ai/en/chinese-subtitle", "https://jable.tv/", "IPX-850; touch /tmp/x", "../../IPX-850"])
def test_not_claimed(source):
    assert not uav.recognizes(source)


def test_code_and_command_input():
    assert _source_from_input("fc2 ppv 1460748") == ("FC2-PPV-1460748", None)
    assert uav.candidates("IPX-850")[0] == "https://missav.ai/en/ipx-850"
    with pytest.raises(ValueError):
        _source_from_input("IPX-850; touch /tmp/x")


def test_process_reports_progress_messages_and_redacts(tmp_path):
    script = tmp_path / "child.py"
    script.write_text('import json\nprint("P20_UAV " + json.dumps({"type":"progress","percent":42,"speed":1024,"eta":None}))\nprint("P20_UAV " + json.dumps({"type":"message","message":"合并完成"}))\nprint("https://example.test/video?token=private")\n')
    progress, messages = [], []
    engines._run([sys.executable, str(script)], lambda: False, lambda *p: progress.append(p), message=messages.append)
    assert (42, 1024, None) in progress
    assert "合并完成" in messages
    assert "private" not in " ".join(messages)


def test_cancel_kills_process_group(tmp_path):
    script = tmp_path / "child.py"
    script.write_text("import time\ntime.sleep(60)\n")
    with pytest.raises(engines.EngineError, match="cancelled"):
        engines._run([sys.executable, str(script)], lambda: True)


def test_engine_rejects_success_without_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(engines, "available_tools", lambda: {"uav": True})
    monkeypatch.setattr(engines, "_run", lambda *a, **k: [])
    with pytest.raises(engines.EngineError, match="no validated video"):
        engines.download("uav", "IPX-850", tmp_path, "video.mp4", {}, lambda: False)
    assert not (tmp_path / ".uav-job.json").exists()


def test_download_task_uses_existing_flash_pipeline(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("test")
    settings = Settings(tmp_path, "127.0.0.1", 0, token, tmp_path / "secrets", "http://127.0.0.1:9", tmp_path / "qq-token")
    settings.ensure_dirs()
    service = TransferService(settings, Store(tmp_path / "transfer.db"))
    uploaded = []

    def fake_download(engine, source, out, filename, headers, cancel, progress, *args, **kwargs):
        assert engine == "uav"
        out.mkdir(parents=True)
        kwargs["message"]("视频校验通过")
        progress(100, 0, 0)
        p = out / "video.mp4"
        p.write_bytes(b"controlled media fixture")
        return p

    def fake_pack(source_path, output, password, volume_size, cancel):
        assert source_path.name == "video.mp4"
        assert password
        output.write_bytes(b"encrypted archive fixture")
        return [output]

    def fake_upload(path, *args, **kwargs):
        uploaded.append(path)
        kwargs["progress"](100, 0, 0)
        return {"ok": True, "upload_complete": True, "share_link": "https://qfile.qq.com/q/test"}

    monkeypatch.setattr("transfer_hub.fetch_adapters.download", fake_download)
    monkeypatch.setattr("transfer_hub.service.pack_zip_aes", fake_pack)
    monkeypatch.setattr("transfer_hub.service.verify_7z_password", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service.flash, "upload", fake_upload)
    job = service.create_url_job({"source": "IPX-850", "source_type": "auto", "pack": False, "encrypt": False, "delivery_backend": "qq_flash", "delivery_profile": "iphone"})
    service._execute(job)
    result = service.store.get_job(job["id"])
    assert result["state"] == "READY"
    assert result["engine"] == "uav"
    assert result["sha256"]
    assert result["options"]["delivery_profile"] == "unified"
    assert result["options"]["encrypt"] is True
    assert uploaded[0].parent == tmp_path / "jobs" / job["id"]
    assert uploaded[0].suffix == ".zip"
    assert any(e["type"] == "job.download_message" for e in service.store.recent_events(job["id"], limit=100))


def test_upload_progress_does_not_hide_download_messages(tmp_path):
    token = tmp_path / "token"
    token.write_text("test")
    settings = Settings(tmp_path, "127.0.0.1", 0, token, tmp_path / "secrets", "http://127.0.0.1:9", tmp_path / "qq-token")
    app = create_app(settings, start_worker=False)
    job = app.state.service.create_url_job({"source": "IPX-850"})
    app.state.store.event(job["id"], "job.download_message", {"message": "视频校验通过"})
    for i in range(35):
        app.state.store.event(job["id"], "job.upload_progress", {"progress": i})
    with TestClient(app) as client:
        events = client.get(f"/api/v1/jobs/{job['id']}/events", headers={"Authorization": "Bearer test"}).json()
    assert any(e["type"] == "job.download_message" for e in events)
