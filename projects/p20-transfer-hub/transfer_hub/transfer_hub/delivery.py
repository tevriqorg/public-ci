from __future__ import annotations

import http.client
import json
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urlparse

UploadProgress = Callable[[float, int, int | None], None]
UploadStarted = Callable[[str], None]


class DeliveryError(RuntimeError):
    pass


class QQFlashDelivery:
    def __init__(self, base_url: str, token_file: Path):
        self.base_url = base_url.rstrip("/")
        self.token_file = token_file

    def _token(self) -> str:
        if not self.token_file.exists():
            raise DeliveryError("QQ Flash token file is missing")
        token = self.token_file.read_text().strip()
        if not token:
            raise DeliveryError("QQ Flash token is empty")
        return token

    def _connection(self, timeout: float):
        parsed = urlparse(self.base_url)
        cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        return parsed, cls(parsed.hostname, parsed.port, timeout=timeout)

    def health_details(self) -> dict:
        try:
            parsed, conn = self._connection(8)
            endpoint = (parsed.path.rstrip("/") if parsed.path else "") + "/health"
            conn.request("GET", endpoint)
            response = conn.getresponse()
            raw = response.read()
            conn.close()
            body = json.loads(raw.decode())
            if response.status != 200 or not body.get("ok"):
                return {"healthy": False, "error": body.get("error") or f"HTTP {response.status}"}
            return {"healthy": True, **body}
        except Exception as exc:
            return {"healthy": False, "error": str(exc)}

    def healthy(self) -> bool:
        return bool(self.health_details().get("healthy"))

    def _cancel(self, file_set_id: str, token: str) -> dict:
        parsed, conn = self._connection(15)
        endpoint = (parsed.path.rstrip("/") if parsed.path else "") + "/v1/cancel"
        payload = json.dumps({"file_set_id": file_set_id}).encode()
        conn.request(
            "POST",
            endpoint,
            body=payload,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            body = json.loads(raw.decode())
        except Exception as exc:
            raise DeliveryError(f"invalid QQ Flash cancel response: HTTP {response.status}") from exc
        if response.status >= 300 or not body.get("ok"):
            raise DeliveryError(body.get("error") or f"QQ Flash cancel HTTP {response.status}")
        return body

    def _status(self, file_set_id: str, token: str) -> dict:
        parsed, conn = self._connection(15)
        endpoint = (parsed.path.rstrip("/") if parsed.path else "") + "/v1/status?" + urlencode({
            "file_set_id": file_set_id,
        })
        conn.request("GET", endpoint, headers={"Authorization": "Bearer " + token})
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            body = json.loads(raw.decode())
        except Exception as exc:
            raise DeliveryError(f"invalid QQ Flash status response: HTTP {response.status}") from exc
        if response.status >= 300 or not body.get("ok"):
            raise DeliveryError(body.get("error") or f"QQ Flash status HTTP {response.status}")
        return body

    def status(self, file_set_id: str) -> dict:
        return self._status(file_set_id, self._token())

    def download_url(self, file_set_id: str, filename: str, size: int, file_index: int = 0) -> dict:
        token = self._token()
        parsed, conn = self._connection(30)
        endpoint = (parsed.path.rstrip("/") if parsed.path else "") + "/v1/download-url"
        payload = json.dumps({
            "file_set_id": file_set_id,
            "filename": filename,
            "size": int(size),
            "file_index": int(file_index),
        }).encode()
        conn.request(
            "POST",
            endpoint,
            body=payload,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            body = json.loads(raw.decode())
        except Exception as exc:
            raise DeliveryError(f"invalid QQ Flash download-url response: HTTP {response.status}") from exc
        if response.status >= 300 or not body.get("ok"):
            raise DeliveryError(body.get("error") or f"QQ Flash download-url HTTP {response.status}")
        return body

    def wait_existing(
        self,
        file_set_id: str,
        progress: UploadProgress | None = None,
        cancel: Callable[[], bool] | None = None,
        timeout: float = 3600,
        poll_interval: float = 0.5,
    ) -> dict:
        token = self._token()
        deadline = time.monotonic() + timeout
        while True:
            if cancel and cancel():
                self._cancel(file_set_id, token)
                raise DeliveryError("cancelled")
            status = self._status(file_set_id, token)
            total = int(status.get("total_size") or 0)
            uploaded = int(status.get("uploaded_size") or 0)
            pct = min(100.0, (uploaded / total * 100.0) if total else 0.0)
            speed = int(status.get("speed") or 0)
            eta_raw = status.get("remain_seconds")
            eta = int(eta_raw) if eta_raw is not None else None
            if progress:
                progress(pct, speed, eta)
            if status.get("sub_type") == "uploaded":
                if progress:
                    progress(100.0, 0, 0)
                return {"ok": True, "file_set_id": file_set_id, "share_link": status.get("share_link"),
                        "upload_complete": True, "flash_status": status}
            if status.get("sub_type") == "cancelled":
                raise DeliveryError(f"QQ Flash upload cancelled: {file_set_id}")
            if status.get("sub_type") == "failed":
                raise DeliveryError(status.get("verify_error") or f"QQ Flash remote verification failed: {file_set_id}")
            if time.monotonic() >= deadline:
                raise DeliveryError(f"QQ Flash upload timeout: {file_set_id}")
            time.sleep(poll_interval)

    def upload(
        self,
        path: Path,
        title: str | None = None,
        filename: str | None = None,
        progress: UploadProgress | None = None,
        started: UploadStarted | None = None,
        idempotency_key: str | None = None,
        cancel: Callable[[], bool] | None = None,
        timeout: float = 3600,
        poll_interval: float = 0.5,
    ) -> dict:
        token = self._token()
        parsed, conn = self._connection(60)
        delivery_name = Path(filename or path.name).name
        query = urlencode({"filename": delivery_name, "title": title or delivery_name, "wait": "0"})
        endpoint = (parsed.path.rstrip("/") if parsed.path else "") + "/v1/upload?" + query
        size = path.stat().st_size
        conn.putrequest("POST", endpoint)
        conn.putheader("Authorization", "Bearer " + token)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(size))
        conn.putheader("X-Filename", delivery_name)
        if idempotency_key:
            conn.putheader("X-Idempotency-Key", idempotency_key)
        conn.endheaders()

        with path.open("rb") as file:
            while True:
                chunk = file.read(1024 * 1024)
                if not chunk:
                    break
                conn.send(chunk)

        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            body = json.loads(raw.decode())
        except Exception as exc:
            raise DeliveryError(
                "QQ Flash (qqxfer-api) is not reachable on port 17600 - the QQ "
                "runtime is probably stopped. Start it first (P20 UI gate, "
                "POST /api/v1/runtime/start, or the check_runtime MCP tool) "
                "and retry the transfer."
            ) from exc
        if response.status >= 300 or not body.get("ok"):
            raise DeliveryError(body.get("error") or f"QQ Flash HTTP {response.status}")

        file_set_id = str(body.get("file_set_id") or "")
        if not file_set_id:
            raise DeliveryError("QQ Flash response has no file_set_id")
        if started:
            started(file_set_id)
        if body.get("upload_complete"):
            if progress:
                progress(100.0, 0, 0)
            return body

        deadline = time.monotonic() + timeout
        while True:
            if cancel and cancel():
                self._cancel(file_set_id, token)
                raise DeliveryError("cancelled")
            status = self._status(file_set_id, token)
            total = int(status.get("total_size") or size or 0)
            uploaded = int(status.get("uploaded_size") or 0)
            pct = min(100.0, (uploaded / total * 100.0) if total else 0.0)
            speed = int(status.get("speed") or 0)
            eta_raw = status.get("remain_seconds")
            eta = int(eta_raw) if eta_raw is not None else None
            if progress:
                progress(pct, speed, eta)

            if status.get("sub_type") == "uploaded":
                body["upload_complete"] = True
                body["flash_status"] = status
                if progress:
                    progress(100.0, 0, 0)
                return body
            if status.get("sub_type") == "failed":
                raise DeliveryError(status.get("verify_error") or f"QQ Flash remote verification failed: {file_set_id}")
            if time.monotonic() >= deadline:
                raise DeliveryError(f"QQ Flash upload timeout: {file_set_id}")
            time.sleep(poll_interval)
