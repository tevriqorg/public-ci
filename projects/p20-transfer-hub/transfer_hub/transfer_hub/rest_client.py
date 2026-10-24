from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class TransferAPIError(RuntimeError):
    pass


class TransferAPIClient:
    def __init__(self, base_url: str | None = None, token_file: str | Path | None = None):
        self.base_url = (base_url or os.getenv("P20_TRANSFER_API_URL", "http://127.0.0.1:17610")).rstrip("/")
        self.token_file = Path(
            token_file
            or os.getenv("P20_TRANSFER_API_TOKEN_FILE", "/var/lib/p20-transfer/api.token")
        )

    def _token(self) -> str:
        token = self.token_file.read_text().strip()
        if not token:
            raise TransferAPIError("Transfer API token is empty")
        return token

    def _request_object(self, method: str, path: str, payload: dict[str, Any] | None = None) -> urllib.request.Request:
        data = None if payload is None else json.dumps(payload).encode()
        return urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": "Bearer " + self._token(),
                "Content-Type": "application/json",
                "User-Agent": "p20-transfer-mcp/0.1",
            },
        )

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        req = self._request_object(method, path, payload)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise TransferAPIError(f"Transfer API HTTP {exc.code}: {body}") from exc
        except OSError as exc:
            raise TransferAPIError(f"Transfer API unavailable: {exc}") from exc
        try:
            return json.loads(raw.decode())
        except Exception as exc:
            raise TransferAPIError("Transfer API returned invalid JSON") from exc

    def request_status(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        req = self._request_object(method, path, payload)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read()
                status = response.status
                headers = dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
            headers = dict(exc.headers.items())
        except OSError as exc:
            return {"ok": False, "status": "UNAVAILABLE", "error": f"Transfer API unavailable: {exc}"}
        try:
            body = json.loads(raw.decode() or "{}")
        except Exception:
            body = {"raw": raw.decode(errors="replace")}
        if status < 300:
            return {"ok": True, "http_status": status, **(body if isinstance(body, dict) else {"data": body})}
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, dict):
            return {"ok": False, "http_status": status, "retry_after": headers.get("Retry-After"), **detail}
        return {"ok": False, "http_status": status, "error": body}
