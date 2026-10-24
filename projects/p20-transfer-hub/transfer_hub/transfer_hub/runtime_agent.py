from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path


class RuntimeAgent:
    """Thin client for the host runtime-control agent (start/stop/status).

    The QQ runtime is optional: when it is off (to save a 2 vCPU host) the
    agent controls bringing it up on demand. Never removes containers.
    """

    def __init__(self, base_url: str, token_file: Path) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_file = token_file

    def _token(self) -> str:
        try:
            return self.token_file.read_text().strip()
        except Exception:
            return ""

    def configured(self) -> bool:
        return bool(self._token())

    def _call(self, method: str, path: str, timeout: float = 200.0) -> dict:
        token = self._token()
        if not token:
            return {"ok": False, "error": "runtime agent token not configured"}
        req = urllib.request.Request(
            self.base_url + path,
            method=method,
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read().decode() or "{}")
            except Exception:
                return {"ok": False, "error": f"HTTP {exc.code}"}
        except Exception as exc:  # connection refused etc. -> runtime is down
            return {"ok": False, "error": str(exc)}

    def status(self) -> dict:
        return self._call("GET", "/status", timeout=20)

    def start(self, timeout: float = 200.0) -> dict:
        return self._call("POST", "/start", timeout=timeout)

    def stop(self) -> dict:
        return self._call("POST", "/stop", timeout=140)


    def browser_status(self) -> dict:
        return self._call("GET", "/browser/status", timeout=20)

    def browser_start(self) -> dict:
        return self._call("POST", "/browser/start", timeout=90)

    def browser_stop(self) -> dict:
        return self._call("POST", "/browser/stop", timeout=60)
