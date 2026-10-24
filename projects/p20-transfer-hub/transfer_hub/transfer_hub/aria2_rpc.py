from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable


class Aria2RPCError(RuntimeError):
    pass


class Aria2RPC:
    def __init__(self, url: str, secret_file: Path) -> None:
        self.url = url
        self.secret_file = secret_file

    def _secret(self) -> str:
        try:
            return self.secret_file.read_text().strip()
        except OSError:
            return ""

    def call(self, method: str, *params: Any, timeout: float = 20) -> Any:
        secret = self._secret()
        rpc_params = list(params)
        if secret:
            rpc_params.insert(0, f"token:{secret}")
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": "p20",
            "method": f"aria2.{method}",
            "params": rpc_params,
        }).encode()
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                payload = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise Aria2RPCError(f"aria2 RPC unavailable: {exc}") from exc
        if payload.get("error"):
            message = payload["error"].get("message") or "aria2 RPC error"
            raise Aria2RPCError(str(message))
        return payload.get("result")

    def add_uri(self, url: str, output_dir: Path, filename: str,
                headers: dict[str, str] | None = None) -> str:
        options: dict[str, str] = {
            "dir": str(output_dir),
            "out": filename,
            "continue": "true",
            "auto-file-renaming": "false",
            "allow-overwrite": "false",
        }
        if headers:
            options["header"] = [f"{key}: {value}" for key, value in headers.items()]
        return str(self.call("addUri", [url], options))

    def tell_status(self, gid: str) -> dict[str, Any]:
        result = self.call(
            "tellStatus",
            gid,
            ["gid", "status", "totalLength", "completedLength", "downloadSpeed", "errorCode", "errorMessage", "files"],
        )
        return result if isinstance(result, dict) else {}

    def pause(self, gid: str) -> None:
        self.call("pause", gid)

    def unpause(self, gid: str) -> None:
        self.call("unpause", gid)

    def remove(self, gid: str) -> None:
        try:
            self.call("forceRemove", gid)
        except Aria2RPCError:
            pass

    def wait(self, gid: str, *, progress: Callable[[float, int, int | None], None] | None = None,
             cancelled: Callable[[], bool] | None = None, poll_interval: float = 0.5) -> Path:
        while True:
            if cancelled and cancelled():
                self.remove(gid)
                raise Aria2RPCError("cancelled")
            status = self.tell_status(gid)
            state = str(status.get("status") or "")
            total = int(status.get("totalLength") or 0)
            completed = int(status.get("completedLength") or 0)
            speed = int(status.get("downloadSpeed") or 0)
            pct = (completed / total * 100.0) if total else 0.0
            eta = int((total - completed) / speed) if total > completed and speed > 0 else None
            if progress:
                progress(pct, speed, eta)
            if state == "complete":
                files = status.get("files") or []
                if not files:
                    raise Aria2RPCError("aria2 completed without a file result")
                path = Path(str(files[0].get("path") or ""))
                if not path:
                    raise Aria2RPCError("aria2 returned an empty file path")
                if progress:
                    progress(100.0, 0, 0)
                return path
            if state == "paused":
                raise Aria2RPCError("paused")
            if state in {"error", "removed"}:
                message = status.get("errorMessage") or status.get("errorCode") or state
                raise Aria2RPCError(f"aria2 {state}: {message}")
            time.sleep(poll_interval)
