"""Fail-closed P2 #126 canary process/data-plane and private credential guard.

Before ANY canary API write or fault injection, check both effective running
process environments against the pinned clone state and private environment.
No secret values are logged. This module never starts or changes services.
"""
import json
import hashlib
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


def verify_binary_digest(path: Path, expected: str):
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("missing independently pinned artifact digest")
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("canary binary path must be regular")
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1024 * 1024):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise ValueError("canary binary digest does not match independent build artifact")
    return True


def listener_owned_by_pid(pid: int, port: int):
    """Linux loopback TCP LISTEN inode must belong to this exact process."""
    if not isinstance(pid, int) or pid < 2 or not 1 <= port <= 65535:
        return False
    inodes = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="ascii") as f:
                next(f)
                for line in f:
                    fields = line.split()
                    addr, sep, raw_port = fields[1].partition(":")
                    if not sep or fields[3] != "0A" or int(raw_port, 16) != port:
                        continue
                    # 0100007F is 127.0.0.1 in /proc/net/tcp little-endian.
                    if table.endswith("/tcp") and addr != "0100007F":
                        continue
                    if table.endswith("/tcp6"):
                        continue  # This canary is IPv4-loopback-only.
                    inodes.add(fields[9])
        except (OSError, ValueError, StopIteration):
            return False
    if len(inodes) != 1:
        return False
    try:
        for fd in (Path("/proc") / str(pid) / "fd").iterdir():
            try:
                if os.readlink(fd) == "socket:[" + next(iter(inodes)) + "]":
                    return True
            except (OSError, PermissionError):
                continue
    except (OSError, PermissionError):
        return False
    return False


def _assert_dedicated_storage(root: Path):
    root = Path(root)
    storage = root / "storage"
    if storage.is_symlink() or not storage.is_dir():
        raise ValueError("canary storage must be a real private directory")
    if storage.resolve() != root.resolve() / "storage":
        raise ValueError("canary storage resolves outside its private root")
    if storage.stat().st_dev != root.stat().st_dev:
        raise ValueError("canary storage crosses filesystem boundary")
    for name in ("storage", "storage/10000", "storage/10000/aibox126"):
        item = root / name
        if item.exists() and (item.is_symlink() or not item.is_dir()
                              or not item.resolve().is_relative_to(storage.resolve())):
            raise ValueError("canary storage hierarchy escapes isolated root")
    # A bind mount may be on the same device: st_dev and realpath alone miss it.
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as f:
            for line in f:
                fields = line.split()
                mountpoint = fields[4].replace("\040", " ")
                if mountpoint == str(storage) or mountpoint.startswith(str(storage) + "/"):
                    raise ValueError("canary storage contains a mount/bind alias")
    except OSError as e:
        raise ValueError("cannot verify canary storage mount isolation") from e


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
    _assert_dedicated_storage(root)
    if set(processes) != {"api", "worker"}:
        raise ValueError("both canary runtime processes must be verified")
    for role, effective in processes.items():
        if effective.get("WEKNORA_RUNTIME_ROLE") != role:
            raise ValueError("effective canary process role mismatch")
        if effective.get("SERVER_HOST") != "127.0.0.1" or effective.get("SERVER_PORT") != ("18082" if role == "api" else "18083"):
            raise ValueError("effective canary process port/bind mismatch")
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
    verify_binary_digest(binary, state.get("binary_sha256"))
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
        if os.stat(process / "exe").st_ino != binary.stat().st_ino or os.stat(process / "exe").st_dev != binary.stat().st_dev:
            raise ValueError("effective process executable inode differs from verified artifact")
        raw = (process / "environ").read_bytes()
        processes[role] = dict(x.split("=", 1) for x in raw.decode().split("\0") if "=" in x)
    verify_canary_runtime(root, state, env, processes)
    api_pid = int(subprocess.check_output(["systemctl", "--user", "show",
                    "aibox126-canary-api", "-p", "MainPID", "--value"],
                    text=True, timeout=5).strip())
    if not listener_owned_by_pid(api_pid, 18082):
        raise ValueError("canary API port is not owned by verified API process")
    return state
