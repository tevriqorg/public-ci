import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from transfer_hub.delivery import DeliveryError, QQFlashDelivery


class FlashHandler(BaseHTTPRequestHandler):
    status_calls = 0
    cancel_calls = 0
    idempotency_keys = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        if self.path == "/v1/cancel":
            type(self).cancel_calls += 1
            payload = json.loads(raw.decode())
            assert payload["file_set_id"] == "test-set"
            self._json(200, {"ok": True, "file_set_id": "test-set", "cancelled": True})
            return
        if self.path == "/v1/download-url":
            payload = json.loads(raw.decode())
            assert payload == {
                "file_set_id": "test-set",
                "filename": "payload.zip",
                "size": 123,
                "file_index": 0,
            }
            self._json(200, {
                "ok": True,
                "file_set_id": "test-set",
                "url": "https://multimedia.qfile.qq.com/download?ok=1",
                "expire_timestamp_seconds": "9999999999",
                "filename": "payload.zip",
                "size": 123,
            })
            return
        assert self.path.startswith("/v1/upload?")
        assert len(raw) == length
        type(self).idempotency_keys.append(self.headers.get("X-Idempotency-Key"))
        body = {
            "ok": True,
            "file_set_id": "test-set",
            "share_link": "https://example.invalid/share",
            "upload_complete": False,
            "flash_status": {"sub_type": "submitted"},
        }
        self._json(202, body)

    def do_GET(self):
        if self.path.startswith("/v1/status?"):
            type(self).status_calls += 1
            if type(self).status_calls == 1:
                body = {
                    "ok": True,
                    "file_set_id": "test-set",
                    "sub_type": "uploading",
                    "uploaded_size": 50,
                    "total_size": 100,
                    "speed": 25,
                    "remain_seconds": 2,
                }
            else:
                body = {
                    "ok": True,
                    "file_set_id": "test-set",
                    "sub_type": "uploaded",
                    "uploaded_size": 100,
                    "total_size": 100,
                    "speed": 0,
                    "remain_seconds": 0,
                    "spool_cleaned": True,
                }
            self._json(200, body)
            return
        self._json(404, {"ok": False})

    def _json(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def test_qq_flash_async_progress(tmp_path: Path):
    FlashHandler.status_calls = 0
    FlashHandler.idempotency_keys = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FlashHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        token = tmp_path / "token"
        token.write_text("secret")
        source = tmp_path / "payload.bin"
        source.write_bytes(b"x" * 100)
        progress = []

        delivery = QQFlashDelivery(f"http://127.0.0.1:{server.server_port}", token)
        result = delivery.upload(
            source,
            "payload",
            progress=lambda pct, speed, eta: progress.append((pct, speed, eta)),
            idempotency_key="job-1:artifact-1",
            poll_interval=0.01,
        )

        assert result["upload_complete"] is True
        assert result["flash_status"]["sub_type"] == "uploaded"
        assert any(pct == 50 for pct, _speed, _eta in progress)
        assert progress[-1][0] == 100
        assert FlashHandler.idempotency_keys == ["job-1:artifact-1"]
    finally:
        server.shutdown()
        server.server_close()


def test_qq_flash_download_url(tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FlashHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        token = tmp_path / "token"
        token.write_text("secret")
        delivery = QQFlashDelivery(f"http://127.0.0.1:{server.server_port}", token)
        result = delivery.download_url("test-set", "payload.zip", 123)
        assert result["url"].startswith("https://multimedia.qfile.qq.com/")
        assert result["expire_timestamp_seconds"] == "9999999999"
    finally:
        server.shutdown()
        server.server_close()


def test_qq_flash_cancel(tmp_path: Path):
    FlashHandler.status_calls = 0
    FlashHandler.cancel_calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), FlashHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        token = tmp_path / "token"
        token.write_text("secret")
        source = tmp_path / "payload.bin"
        source.write_bytes(b"x" * 100)
        checks = {"count": 0}

        def cancel():
            checks["count"] += 1
            return checks["count"] >= 2

        delivery = QQFlashDelivery(f"http://127.0.0.1:{server.server_port}", token)
        try:
            delivery.upload(source, "payload", cancel=cancel, poll_interval=0.01)
            raise AssertionError("expected DeliveryError")
        except DeliveryError as exc:
            assert str(exc) == "cancelled"

        assert FlashHandler.cancel_calls == 1
    finally:
        server.shutdown()
        server.server_close()
