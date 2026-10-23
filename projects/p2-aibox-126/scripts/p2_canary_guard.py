"""Fail-closed P2 #126 canary process/data-plane and private credential guard.

Before ANY canary API write or fault injection, check both effective running
process environments against the pinned clone state and private environment.
No secret values are logged. This module never starts or changes services.
"""
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess

SAFE_KEYS = (
    "DB_NAME", "DB_HOST", "DB_PORT", "REDIS_ADDR", "REDIS_DB",
    "REDIS_PREFIX", "WEKNORA_REDIS_NAMESPACE", "LOCAL_STORAGE_BASE_DIR",
    "AIBOX_INGRESS_ALLOWED_RESOURCE_PREFIX",
)


def secure_canary_read(root: Path, path: Path) -> str:
    root = Path(root)
    path = Path(path)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("unsafe canary root")
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError("canary root must be private to the current user")
    if path.parent != root or path.is_symlink():
        raise ValueError("credential must be a regular direct child of private canary root")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError("canary file must be owner-only regular file")
        if st.st_size > 2_000_000:
            raise ValueError("canary file exceeds private read cap")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as handle:
            data = handle.read(2_000_001)
        if len(data) > 2_000_000:
            raise ValueError("canary file exceeds private read cap")
        return data
    finally:
        os.close(fd)


def parse_private_env(text: str):
    result = {}
    for line in text.splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("invalid private environment line")
        k, v = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", k) or k in result:
            raise ValueError("invalid or duplicate environment key")
        parts = shlex.split(v)
        if len(parts) != 1:
            raise ValueError("ambiguous private environment value")
        result[k] = parts[0]
    return result


def verify_canary_runtime(root: Path, state: dict, env: dict, processes: dict):
    """Validate actual api+worker effective environment, not only unit config."""
    root = Path(root)
    clone = state.get("clone_db", "")
    original = state.get("original_db", "")
    if (not isinstance(clone, str) or not re.fullmatch(r"aibox126_canary_[a-zA-Z0-9_]+", clone)
            or clone == original or not original):
        raise ValueError("clone DB identity invalid")
    if env.get("DB_NAME") != clone:
        raise ValueError("canary database is not pinned to clone")
    if env.get("REDIS_DB") not in {str(i) for i in range(1, 16)}:
        raise ValueError("canary Redis DB must be isolated and non-default")
    if env.get("REDIS_PREFIX") != "aibox126-canary" or env.get("WEKNORA_REDIS_NAMESPACE") != "aibox126-canary":
        raise ValueError("canary Redis namespace mismatch")
    if env.get("REDIS_ADDR") != "127.0.0.1:56379":
        raise ValueError("canary Redis endpoint mismatch")
    if env.get("AIBOX_INGRESS_ALLOWED_RESOURCE_PREFIX") != "local://10000/aibox126/":
        raise ValueError("canary raw path allowlist mismatch")
    if env.get("LOCAL_STORAGE_BASE_DIR") != str(root / "storage"):
        raise ValueError("canary storage must be dedicated")
    if set(processes) != {"api", "worker"}:
        raise ValueError("both canary runtime processes must be verified")
    for role, effective in processes.items():
        if effective.get("WEKNORA_RUNTIME_ROLE") != role:
            raise ValueError("effective canary process role mismatch")
        if role == "worker" and "core" not in effective.get("WEKNORA_WORKER_POOLS", "").split(","):
            raise ValueError("canary core worker is not active")
        for k, expected in env.items():
            # Check ALL effective fields, including private credentials, without logging.
            if effective.get(k) != expected:
                raise ValueError("effective canary process environment differs from private clone manifest")
        for k in SAFE_KEYS:
            if effective.get(k) != env.get(k):
                raise ValueError("effective canary data-plane mismatch")
    return True


def verify_live_canary(root: Path):
    root = Path(root)
    state = json.loads(secure_canary_read(root, root / "canary-state.json"))
    env = parse_private_env(secure_canary_read(root, root / "canary.env"))
    processes = {}
    source_sha = state.get("source_sha", "")
    if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("canary source commit pin missing")
    binary = root / "releases" / source_sha / "weknora-server"
    if not binary.is_file():
        raise ValueError("canary pinned artifact unavailable")
    for role in ("api", "worker"):
        unit = "aibox126-canary-" + role
        pid = subprocess.check_output(["systemctl", "--user", "show", unit,
                                       "-p", "MainPID", "--value"],
                                      text=True, timeout=5).strip()
        if not pid.isdecimal() or int(pid) <= 1:
            raise ValueError("canary service not running")
        process = Path("/proc") / pid
        if (process / "exe").resolve() != binary:
            raise ValueError("effective canary executable differs from exact artifact")
        raw = (process / "environ").read_bytes()
        processes[role] = dict(x.split("=", 1) for x in raw.decode().split("\0") if "=" in x)
    verify_canary_runtime(root, state, env, processes)
    return state
