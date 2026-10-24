from __future__ import annotations

import secrets
import shutil
import subprocess
import time
from pathlib import Path

from .aria2_rpc import Aria2RPC


class Aria2Daemon:
    def __init__(self, data_dir: Path, rpc: Aria2RPC, concurrency: int) -> None:
        self.data_dir = data_dir
        self.rpc = rpc
        self.concurrency = max(1, concurrency)
        self.process: subprocess.Popen | None = None

    def _ensure_secret(self) -> str:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        secret_file = self.rpc.secret_file
        if not secret_file.exists():
            secret_file.write_text(secrets.token_urlsafe(32))
            secret_file.chmod(0o600)
        return secret_file.read_text().strip()

    def start(self) -> None:
        # Unit-test/dev hosts may not have aria2c; the production image does.
        # Defer the failure until an aria2-backed FETCH is actually requested.
        if shutil.which("aria2c") is None:
            return
        try:
            self.rpc.call("getVersion", timeout=2)
            return
        except Exception:
            pass
        secret = self._ensure_secret()
        session = self.data_dir / "aria2.session"
        session.touch(exist_ok=True)
        log = (self.data_dir / "aria2.log").open("ab", buffering=0)
        argv = [
            "aria2c",
            "--enable-rpc=true",
            "--rpc-listen-all=false",
            "--rpc-listen-port=17618",
            f"--rpc-secret={secret}",
            f"--input-file={session}",
            f"--save-session={session}",
            "--save-session-interval=30",
            "--auto-save-interval=30",
            "--continue=true",
            "--always-resume=true",
            "--auto-file-renaming=false",
            f"--max-concurrent-downloads={self.concurrency}",
            "--summary-interval=0",
            "--console-log-level=warn",
            "--file-allocation=none",
        ]
        self.process = subprocess.Popen(argv, stdout=log, stderr=log, start_new_session=True)
        for _ in range(50):
            if self.process.poll() is not None:
                raise RuntimeError("aria2 daemon exited during startup")
            try:
                self.rpc.call("getVersion", timeout=1)
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("aria2 daemon did not become ready")

    def stop(self) -> None:
        try:
            self.rpc.call("saveSession", timeout=3)
        except Exception:
            pass
        process = self.process
        self.process = None
        if not process or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
