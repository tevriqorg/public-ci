from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    bind: str
    port: int
    api_token_file: Path
    secrets_dir: Path
    qqxfer_url: str
    qqxfer_token_file: Path
    runtime_agent_url: str = "http://127.0.0.1:17613"
    runtime_agent_token_file: Path = Path("/var/lib/p20-transfer/runtime-agent.token")
    aria2_url: str = "http://127.0.0.1:17618/jsonrpc"
    aria2_secret_file: Path = Path("/var/lib/p20-transfer/aria2/rpc.secret")
    worker_poll_seconds: float = 0.8
    fetch_concurrency: int = 2
    process_concurrency: int = 1
    delivery_concurrency: int = 1
    stage_lease_seconds: int = 60
    public_url: str | None = None
    cmcloud_authorization_file: Path | None = None
    cmcloud_state_file: Path | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("P20_TRANSFER_DATA", "/var/lib/p20-transfer")).resolve()
        return cls(
            data_dir=data_dir,
            bind=os.getenv("P20_TRANSFER_BIND", "127.0.0.1"),
            port=int(os.getenv("P20_TRANSFER_PORT", "17610")),
            api_token_file=Path(os.getenv("P20_TRANSFER_API_TOKEN_FILE", str(data_dir / "api.token"))),
            secrets_dir=Path(os.getenv("P20_TRANSFER_SECRETS_DIR", str(data_dir / "secrets"))),
            qqxfer_url=os.getenv("P20_QQXFER_URL", "http://100.84.55.11:17600"),
            qqxfer_token_file=Path(os.getenv("P20_QQXFER_TOKEN_FILE", "/home/ubuntu/qqxfer-api/api.token")),
            runtime_agent_url=os.getenv("P20_RUNTIME_AGENT_URL", "http://127.0.0.1:17613"),
            runtime_agent_token_file=Path(os.getenv("P20_RUNTIME_AGENT_TOKEN_FILE", str(data_dir / "runtime-agent.token"))),
            aria2_url=os.getenv("P20_ARIA2_URL", "http://127.0.0.1:17618/jsonrpc"),
            aria2_secret_file=Path(os.getenv("P20_ARIA2_SECRET_FILE", str(data_dir / "aria2" / "rpc.secret"))),
            fetch_concurrency=max(1, int(os.getenv("P20_FETCH_CONCURRENCY", "2"))),
            process_concurrency=max(1, int(os.getenv("P20_PROCESS_CONCURRENCY", "1"))),
            delivery_concurrency=max(1, int(os.getenv("P20_DELIVERY_CONCURRENCY", "1"))),
            stage_lease_seconds=max(1, int(os.getenv("P20_STAGE_LEASE_SECONDS", "60"))),
            public_url=os.getenv("P20_TRANSFER_PUBLIC_URL") or None,
            cmcloud_authorization_file=Path(os.getenv("P20_CMCLOUD_AUTHORIZATION_FILE", str(data_dir / "secrets" / "cmcloud.authorization"))),
            cmcloud_state_file=Path(os.getenv("P20_CMCLOUD_STATE_FILE", str(data_dir / "cmcloud" / "state.json"))),
        )

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.data_dir / "jobs",
            self.data_dir / "uploads",
            self.data_dir / "local-files",
            self.data_dir / "aria2",
            self.secrets_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def local_file_roots(self) -> dict[str, Path]:
        roots: dict[str, Path] = {"local-files": (self.data_dir / "local-files").resolve()}
        for raw in os.getenv("P20_LOCAL_FILE_ROOTS", "").split(os.pathsep):
            raw = raw.strip()
            if not raw:
                continue
            path = Path(raw).resolve()
            base = path.name or "vps-root"
            alias = base
            suffix = 2
            while alias in roots and roots[alias] != path:
                alias = f"{base}-{suffix}"
                suffix += 1
            roots[alias] = path
        return roots
