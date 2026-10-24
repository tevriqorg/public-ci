from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import p20_receive as receiver
from transfer_hub.api import create_app
from transfer_hub.config import Settings
from transfer_hub.receiver_ticket import build_receiver_ticket


def _delivery(name: str = "test.bin", data: bytes = b"payload") -> dict:
    return {
        "backend": "qq_flash",
        "items": [{
            "ok": True,
            "filename": name,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "share_link": "https://qfile.qq.com/q/example",
            "file_set_id": "test-set",
        }],
    }


def test_build_receiver_ticket() -> None:
    job = {"id": "abc", "state": "READY", "result": _delivery()}
    ticket = build_receiver_ticket(job, "http://tovps.example:17610/")
    assert ticket["schema"] == receiver.TICKET_SCHEMA
    assert ticket["artifacts"][0]["sha256"] == job["result"]["items"][0]["sha256"]
    assert ticket["artifacts"][0]["source"]["file_set_id"] == "test-set"
    assert ticket["ack"]["url"] == "http://tovps.example:17610/api/v1/jobs/abc/ack"


def test_unified_receiver_ticket_exposes_archive_postprocess() -> None:
    job = {
        "id": "abc", "state": "READY", "result": _delivery("secure.zip"),
        "options": {"delivery_profile": "unified", "pack": True, "encrypt": True, "pack_format": "zip-aes256"},
        "secret": {"archive_password": "test-secret"},
    }
    ticket = build_receiver_ticket(job, "http://hub/")
    assert ticket["postprocess"] == {
        "kind": "archive", "format": "zip-aes256", "encrypted": True,
        "password": "test-secret", "primary": "secure.zip", "remove_archive_after_success": True,
    }


def test_legacy_mobile_receiver_ticket_without_pack_stays_direct() -> None:
    job = {
        "id": "abc", "state": "READY", "result": _delivery("legacy.apk"),
        "options": {"delivery_profile": "android", "pack": False, "encrypt": False},
    }
    ticket = build_receiver_ticket(job, "http://hub/")
    assert ticket["postprocess"] == {"kind": "none"}


def test_receiver_ticket_rejects_non_ready() -> None:
    with pytest.raises(ValueError, match="READY"):
        build_receiver_ticket({"id": "abc", "state": "DONE", "result": _delivery()}, "http://hub/")


def test_api_receiver_ticket_uses_public_url(tmp_path: Path) -> None:
    token = tmp_path / "api.token"
    token.write_text("test-token")
    settings = Settings(
        data_dir=tmp_path,
        bind="127.0.0.1",
        port=0,
        api_token_file=token,
        secrets_dir=tmp_path / "secrets",
        qqxfer_url="http://127.0.0.1:9",
        qqxfer_token_file=tmp_path / "missing",
        public_url="http://tovps.example:17610",
    )
    app = create_app(settings=settings, start_worker=False)
    job = app.state.service.create_url_job({
        "source": "https://example.com/a.bin",
        "source_type": "url",
        "delivery_backend": "none",
    })
    app.state.store.update(job["id"], state="READY", result=_delivery())
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        response = client.get(f"/api/v1/jobs/{job['id']}/receiver-ticket", headers=headers)
    assert response.status_code == 200
    assert response.json()["ack"]["url"].startswith("http://tovps.example:17610/")


def test_qfile_nuxt_parser_minimal() -> None:
    root = [
        ["ShallowReactive", 1], {"data": 2}, ["ShallowReactive", 3],
        {"trpc.file.FileFlashTrans/GetFileList_{}": 4}, {"data": 5}, {"file_lists": 6},
        [7], {"file_list": 8}, [9],
        {"physical": 10, "name": 11, "file_size": 12, "file_sha1": 13, "file_md5": 14},
        {"id": 15}, "prefix-test.bin", "7", "sha1-value", "md5-value", "physical-id",
    ]
    html = (
        '<script type="application/json" data-nuxt-data="nuxt-app" '
        f'id="__NUXT_DATA__">{json.dumps(root)}</script>'
    )
    items = receiver._extract_qfile_items(html)
    assert items == [{
        "name": "prefix-test.bin", "size": 7, "physical_id": "physical-id",
        "sha1": "sha1-value", "md5": "md5-value",
    }]


class _DownloadHandler(BaseHTTPRequestHandler):
    payload = b""
    honor_range = True
    seen_ranges: list[str | None] = []
    ack_count = 0

    def log_message(self, *_args) -> None:
        return

    def do_GET(self) -> None:
        if self.path != "/file":
            self.send_error(404)
            return
        range_header = self.headers.get("Range")
        type(self).seen_ranges.append(range_header)
        payload = type(self).payload
        if range_header and type(self).honor_range:
            start = int(range_header.split("=")[1].split("-")[0])
            body = payload[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(payload)-1}/{len(payload)}")
        else:
            body = payload
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/ack":
            self.send_error(404)
            return
        type(self).ack_count += 1
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


@pytest.fixture
def download_server():
    _DownloadHandler.payload = b""
    _DownloadHandler.honor_range = True
    _DownloadHandler.seen_ranges = []
    _DownloadHandler.ack_count = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DownloadHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _ticket(data: bytes, base: str) -> dict:
    return {
        "schema": receiver.TICKET_SCHEMA,
        "transfer_id": "test-transfer",
        "artifacts": [{
            "name": "test.bin", "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "source": {"kind": "qq_flash_share", "share_link": "https://qfile.qq.com/q/example"},
        }],
        "ack": {"required": True, "method": "POST", "url": base + "/ack"},
    }


def _patch_qq(monkeypatch, base: str, data: bytes) -> None:
    monkeypatch.setattr(receiver, "_fetch_qfile_items", lambda _share: [
        {"name": "prefix-test.bin", "size": len(data), "physical_id": "pid", "sha1": "", "md5": ""}
    ])
    monkeypatch.setattr(receiver, "_signed_download_url", lambda _item: base + "/file")
    original = receiver._safe_http_url

    def allow_local(value: str, *, host: str | None = None) -> str:
        if value.startswith(base):
            return value
        return original(value, host=host)

    monkeypatch.setattr(receiver, "_safe_http_url", allow_local)


def test_resume_range_and_ack(tmp_path: Path, monkeypatch, download_server) -> None:
    data = b"0123456789" * 1000
    _DownloadHandler.payload = data
    base = f"http://127.0.0.1:{download_server.server_address[1]}"
    _patch_qq(monkeypatch, base, data)
    (tmp_path / "test.bin.part").write_bytes(data[:1234])
    result = receiver.receive(_ticket(data, base), tmp_path)
    assert (tmp_path / "test.bin").read_bytes() == data
    assert _DownloadHandler.seen_ranges == ["bytes=1234-"]
    assert _DownloadHandler.ack_count == 1
    assert result["acked"] is True


def test_ignored_range_restarts_safely(tmp_path: Path, monkeypatch, download_server) -> None:
    data = b"abcdef" * 2000
    _DownloadHandler.payload = data
    _DownloadHandler.honor_range = False
    base = f"http://127.0.0.1:{download_server.server_address[1]}"
    _patch_qq(monkeypatch, base, data)
    (tmp_path / "test.bin.part").write_bytes(data[:777])
    receiver.receive(_ticket(data, base), tmp_path)
    assert (tmp_path / "test.bin").read_bytes() == data
    assert _DownloadHandler.seen_ranges == ["bytes=777-"]
    assert _DownloadHandler.ack_count == 1


def test_hash_mismatch_never_acks(tmp_path: Path, monkeypatch, download_server) -> None:
    data = b"correct payload"
    _DownloadHandler.payload = data
    base = f"http://127.0.0.1:{download_server.server_address[1]}"
    _patch_qq(monkeypatch, base, data)
    ticket = _ticket(data, base)
    ticket["artifacts"][0]["sha256"] = "0" * 64
    with pytest.raises(receiver.ReceiverError, match="SHA256"):
        receiver.receive(ticket, tmp_path)
    assert _DownloadHandler.ack_count == 0


def test_existing_verified_file_only_acks(tmp_path: Path, monkeypatch, download_server) -> None:
    data = b"already here"
    base = f"http://127.0.0.1:{download_server.server_address[1]}"
    _patch_qq(monkeypatch, base, data)
    (tmp_path / "test.bin").write_bytes(data)
    result = receiver.receive(_ticket(data, base), tmp_path)
    assert result["artifacts"][0]["status"] == "existing"
    assert _DownloadHandler.seen_ranges == []
    assert _DownloadHandler.ack_count == 1


def test_v1_ticket_remains_compatible(tmp_path: Path, monkeypatch, download_server) -> None:
    data = b"legacy-ticket"
    _DownloadHandler.payload = data
    base = f"http://127.0.0.1:{download_server.server_address[1]}"
    _patch_qq(monkeypatch, base, data)
    ticket = _ticket(data, base)
    ticket["schema"] = "p20.receiver-ticket/v1"
    result = receiver.receive(ticket, tmp_path)
    assert result["postprocess"]["status"] == "not_required"
    assert _DownloadHandler.ack_count == 1


def test_v2_archive_postprocess_happens_before_ack(tmp_path: Path, monkeypatch, download_server) -> None:
    data = b"encrypted-archive-placeholder"
    _DownloadHandler.payload = data
    base = f"http://127.0.0.1:{download_server.server_address[1]}"
    _patch_qq(monkeypatch, base, data)
    ticket = _ticket(data, base)
    ticket["postprocess"] = {
        "kind": "archive", "format": "zip-aes256", "encrypted": True,
        "password": "test-password", "primary": "test.bin", "remove_archive_after_success": True,
    }
    monkeypatch.setattr(receiver.shutil, "which", lambda _name: "/usr/bin/7zz")

    def fake_password_run(args, password):
        out_arg = next(arg for arg in args if arg.startswith("-o"))
        extract_dir = Path(out_arg[2:])
        (extract_dir / "ready.txt").write_text("ready")
        assert password == "test-password"
        assert password not in " ".join(args)
        assert _DownloadHandler.ack_count == 0
        return 0, "Everything is Ok"

    monkeypatch.setattr(receiver, "_run_7z_password", fake_password_run)
    result = receiver.receive(ticket, tmp_path)
    assert (tmp_path / "ready.txt").read_text() == "ready"
    assert not (tmp_path / "test.bin").exists()
    assert result["postprocess"]["status"] == "extracted"
    assert _DownloadHandler.ack_count == 1


def test_v2_extract_failure_never_acks_and_keeps_archive(tmp_path: Path, monkeypatch, download_server) -> None:
    data = b"encrypted-archive-placeholder"
    _DownloadHandler.payload = data
    base = f"http://127.0.0.1:{download_server.server_address[1]}"
    _patch_qq(monkeypatch, base, data)
    ticket = _ticket(data, base)
    ticket["postprocess"] = {
        "kind": "archive", "format": "zip-aes256", "encrypted": True,
        "password": "wrong", "primary": "test.bin", "remove_archive_after_success": True,
    }
    monkeypatch.setattr(receiver.shutil, "which", lambda _name: "/usr/bin/7zz")

    monkeypatch.setattr(receiver, "_run_7z_password", lambda args, password: (2, "wrong password"))
    with pytest.raises(receiver.ReceiverError, match="extraction failed"):
        receiver.receive(ticket, tmp_path)
    assert (tmp_path / "test.bin").exists()
    assert _DownloadHandler.ack_count == 0


def test_keep_archive_after_extract(tmp_path: Path, monkeypatch, download_server) -> None:
    data = b"archive"
    _DownloadHandler.payload = data
    base = f"http://127.0.0.1:{download_server.server_address[1]}"
    _patch_qq(monkeypatch, base, data)
    ticket = _ticket(data, base)
    ticket["postprocess"] = {"kind": "archive", "primary": "test.bin", "password": "pw"}
    monkeypatch.setattr(receiver.shutil, "which", lambda _name: "/usr/bin/7z")

    def fake_password_run(args, password):
        extract_dir = Path(next(arg for arg in args if arg.startswith("-o"))[2:])
        (extract_dir / "out.bin").write_bytes(b"ok")
        assert password == "pw"
        assert password not in " ".join(args)
        return 0, "Everything is Ok"

    monkeypatch.setattr(receiver, "_run_7z_password", fake_password_run)
    receiver.receive(ticket, tmp_path, keep_archive=True)
    assert (tmp_path / "test.bin").exists()
    assert (tmp_path / "out.bin").exists()


def test_qfile_resolve_retries_and_reports_progress(monkeypatch) -> None:
    calls = {"fetch": 0}
    progress: list[str] = []
    sleeps: list[int] = []

    def flaky_fetch(_share: str):
        calls["fetch"] += 1
        if calls["fetch"] < 3:
            raise receiver.ReceiverError("QQ share page contains no downloadable files")
        return [{"name": "prefix-test.bin", "size": 7, "physical_id": "pid", "sha1": "", "md5": ""}]

    monkeypatch.setattr(receiver, "_fetch_qfile_items", flaky_fetch)
    monkeypatch.setattr(receiver, "_signed_download_url", lambda _item: "https://multimedia.qfile.qq.com/download?ok=1")
    monkeypatch.setattr(receiver.time, "sleep", lambda seconds: sleeps.append(seconds))
    url = receiver._resolve_qfile_download_url("https://qfile.qq.com/q/example", "test.bin", 7, progress.append)

    assert url.startswith("https://multimedia.qfile.qq.com/")
    assert calls["fetch"] == 3
    assert sleeps == [1, 2]
    assert "第 1/4 次重试" in progress[0]
    assert "第 2/4 次重试" in progress[1]
    assert progress[-1] == "QQ 下载地址已同步，继续接收…"


def test_receiver_url_reports_retryable_503(tmp_path: Path, monkeypatch) -> None:
    token = tmp_path / "api.token"
    token.write_text("test-token")
    settings = Settings(
        data_dir=tmp_path,
        bind="127.0.0.1",
        port=0,
        api_token_file=token,
        secrets_dir=tmp_path / "secrets",
        qqxfer_url="http://127.0.0.1:9",
        qqxfer_token_file=tmp_path / "missing",
        public_url="http://tovps.example:17610",
    )
    app = create_app(settings=settings, start_worker=False)
    job = app.state.service.create_url_job({"source": "https://example.com/a.bin", "source_type": "url", "delivery_backend": "none"})
    app.state.store.update(job["id"], state="READY", result=_delivery())
    monkeypatch.setattr(receiver, "_fetch_qfile_items", lambda _share: (_ for _ in ()).throw(receiver.ReceiverError("not ready")))
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        response = client.get(f"/api/v1/jobs/{job['id']}/receiver-url?artifact=0", headers=headers)
    assert response.status_code == 503
    assert int(response.headers["retry-after"]) >= 1
    detail = response.json()["detail"]
    assert detail["status"] == "WAITING_FOR_QQ_SHARE"
    assert detail["code"] == "qq_public_share_not_ready"
    assert detail["retryable"] is True
    assert detail["alternatives"][0]["action"] == "retry_later"
    assert "无需重新创建任务" in detail["alternatives"][0]["message"]


def test_receiver_url_prefers_runtime_direct_url(tmp_path: Path, monkeypatch) -> None:
    token = tmp_path / "api.token"
    token.write_text("test-token")
    settings = Settings(
        data_dir=tmp_path,
        bind="127.0.0.1",
        port=0,
        api_token_file=token,
        secrets_dir=tmp_path / "secrets",
        qqxfer_url="http://127.0.0.1:9",
        qqxfer_token_file=tmp_path / "qq.token",
        public_url="http://tovps.example:17610",
    )
    settings.qqxfer_token_file.write_text("qq-token")
    app = create_app(settings=settings, start_worker=False)
    job = app.state.service.create_url_job({"source": "https://example.com/a.bin", "source_type": "url", "delivery_backend": "none"})
    app.state.store.update(job["id"], state="READY", result=_delivery())
    monkeypatch.setattr(app.state.service.flash, "health_details", lambda: {"healthy": True})
    monkeypatch.setattr(app.state.service.flash, "download_url", lambda *_args, **_kwargs: {
        "url": "https://multimedia.qfile.qq.com:443/download?native=1",
        "expire_timestamp_seconds": "9999999999",
    })
    monkeypatch.setattr(receiver, "_fetch_qfile_items", lambda _share: (_ for _ in ()).throw(AssertionError("public fallback should not run")))
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        response = client.get(f"/api/v1/jobs/{job['id']}/receiver-url?artifact=0", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "RECEIVER_READY"
    assert body["resolver"] == "qq_runtime"
    assert body["url"].startswith("https://multimedia.qfile.qq.com:443/")


def test_receiver_url_transport_failure_falls_back_to_public_share(tmp_path: Path, monkeypatch) -> None:
    token = tmp_path / "api.token"
    token.write_text("test-token")
    settings = Settings(
        data_dir=tmp_path,
        bind="127.0.0.1",
        port=0,
        api_token_file=token,
        secrets_dir=tmp_path / "secrets",
        qqxfer_url="http://127.0.0.1:9",
        qqxfer_token_file=tmp_path / "qq.token",
        public_url="http://tovps.example:17610",
    )
    settings.qqxfer_token_file.write_text("qq-token")
    app = create_app(settings=settings, start_worker=False)
    job = app.state.service.create_url_job({"source": "https://example.com/a.bin", "source_type": "url", "delivery_backend": "none"})
    app.state.store.update(job["id"], state="READY", result=_delivery())
    monkeypatch.setattr(app.state.service.flash, "health_details", lambda: {"healthy": True})
    monkeypatch.setattr(app.state.service.flash, "download_url", lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionRefusedError(111, "refused")))
    monkeypatch.setattr(receiver, "_fetch_qfile_items", lambda _share: [{"name": "a.bin", "size": 7, "physical_id": "pid", "sha1": "", "md5": ""}])
    monkeypatch.setattr(receiver, "_signed_download_url", lambda _item: "https://multimedia.qfile.qq.com/download?fallback=1")
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        response = client.get(f"/api/v1/jobs/{job['id']}/receiver-url?artifact=0", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "RECEIVER_READY"
    assert body["resolver"] == "qq_public_share"
    assert body["url"].endswith("fallback=1")
