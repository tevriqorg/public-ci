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
    if env.get("AUTO_MIGRATE") != "false":
        raise ValueError("replica startup migrations must be explicitly disabled")
    if env.get("DB_HOST") != "127.0.0.1" or env.get("DB_PORT") != "55432":
        raise ValueError("canary DB endpoint is outside reviewed container mapping")
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


def verify_release_tree(release: Path, state: dict):
    """Bind ALL runtime files to an independently pinned CI artifact inventory."""
    release = Path(release)
    if release.is_symlink() or not release.is_dir():
        raise ValueError("runtime release directory is unsafe")
    manifest = release / "SHA256SUMS"
    verify_binary_digest(manifest, state.get("release_manifest_sha256"))
    if manifest.stat().st_size > 2_000_000:
        raise ValueError("runtime inventory exceeds cap")
    listed = set()
    for row in manifest.read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (?:\./)?(.+)", row)
        if not match:
            raise ValueError("invalid runtime inventory row")
        digest, name = match.groups()
        part = Path(name)
        if part.is_absolute() or ".." in part.parts or name in listed or name == "SHA256SUMS":
            raise ValueError("unsafe or duplicate runtime inventory path")
        path = release / part
        if any(parent.is_symlink() for parent in [path, *path.parents] if parent != release.parent):
            raise ValueError("runtime inventory contains symlink")
        verify_binary_digest(path, digest)
        listed.add(name)
    actual = set()
    for path in release.rglob("*"):
        if path.is_symlink():
            raise ValueError("runtime release contains symlink")
        if path.is_file() and path != manifest:
            actual.add(path.relative_to(release).as_posix())
    if actual != listed:
        raise ValueError("runtime inventory omits or adds files")
    required = {"weknora-server", "SOURCE_REVISION", "config/config.yaml"}
    if not required <= listed:
        raise ValueError("runtime artifact missing required components")
    if (release / "SOURCE_REVISION").read_text().strip() != state.get("source_sha"):
        raise ValueError("runtime tree source differs from binary source")
    versions = [int(m.group(1)) for name in listed
                if (m := re.fullmatch(r"migrations/versioned/([0-9]+)_.+\.up\.sql", name))]
    if not versions or max(versions) != state.get("schema_version"):
        raise ValueError("runtime schema version is not independently pinned")
    return max(versions)


LAUNCH_PROPERTIES = ("FragmentPath", "DropInPaths", "ExecStart", "WorkingDirectory",
                     "EnvironmentFiles", "Environment", "NeedDaemonReload")


def validate_next_start(root, state, role, properties, files):
    """Validate the *loaded next launch* and the bytes it will read before restart."""
    if role not in {"api", "worker"} or set(properties) != set(LAUNCH_PROPERTIES):
        raise ValueError("incomplete next-start unit snapshot")
    if properties["NeedDaemonReload"] != "no":
        raise ValueError("unit files changed without daemon reload")
    release = Path(root) / "releases" / state["source_sha"]
    binary = str(release / "weknora-server")
    if properties["WorkingDirectory"] != str(release):
        raise ValueError("next-start cwd is not exact artifact")
    command = re.search(r"path=([^ ;]+) ; argv\[\]=([^;]+) ;", properties["ExecStart"])
    if properties["ExecStart"].count("{") != 1 or properties["ExecStart"].count("}") != 1:
        raise ValueError("multiple next-start commands are forbidden")
    if not command or command.group(1) != binary or shlex.split(command.group(2)) != [binary]:
        raise ValueError("next-start executable differs from reviewed artifact")
    rawfiles = properties["EnvironmentFiles"]
    env_paths = re.findall(r"(/[^ ]+) \(ignore_errors=no\)", rawfiles)
    if " ".join(p + " (ignore_errors=no)" for p in env_paths) != rawfiles:
        raise ValueError("ambiguous or optional next-start environment file")
    paths = [properties["FragmentPath"], *shlex.split(properties["DropInPaths"]), *env_paths]
    if set(paths) != set(files):
        raise ValueError("next-start file inventory mismatch")
    effective = dict(item.split("=", 1) for item in shlex.split(properties["Environment"]))
    for path in env_paths:
        effective.update(parse_private_env(files[path]))
    if effective.get("AUTO_MIGRATE") != "false":
        raise ValueError("next-start would enable migrations")
    if effective.get("WEKNORA_RUNTIME_ROLE") != role or effective.get("SERVER_HOST") != "127.0.0.1" or effective.get("SERVER_PORT") != ("18082" if role == "api" else "18083"):
        raise ValueError("next-start role or listener changed")
    normalized = dict(properties)
    normalized["ExecStart"] = binary  # Exclude volatile PID/start/exit fields from systemctl show.
    fingerprint = hashlib.sha256(json.dumps({"properties": normalized,
        "files_sha256": {p: hashlib.sha256(v.encode()).hexdigest() for p,v in files.items()}},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if state.get("launch_snapshot_sha256", {}).get(role) != fingerprint:
        raise ValueError("next-start configuration lacks independent exact pin")
    return True


def verify_next_start(root, state, role="worker"):
    root = Path(root)
    command = ["systemctl", "--user", "show", "aibox126-canary-" + role]
    for name in LAUNCH_PROPERTIES:
        command.extend(["-p", name])
    raw = subprocess.check_output(command, text=True, timeout=10)
    props = dict(line.split("=",1) for line in raw.splitlines() if "=" in line)
    paths = [props.get("FragmentPath", ""), *shlex.split(props.get("DropInPaths", "")),
             *re.findall(r"(/[^ ]+) \(ignore_errors=no\)", props.get("EnvironmentFiles", ""))]
    files = {}
    unitroot = Path.home() / ".config/systemd/user"
    for name in paths:
        path = Path(name)
        if path.is_symlink() or not (path.is_relative_to(root) or path.is_relative_to(unitroot)):
            raise ValueError("next-start file outside allowed private/config roots")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022 or st.st_size > 2_000_000:
                raise ValueError("unsafe next-start configuration file")
            if path.is_relative_to(root) and st.st_mode & 0o077:
                raise ValueError("next-start private file is not owner-only")
            with os.fdopen(fd, "r", closefd=False) as f:
                files[name] = f.read(2_000_001)
        finally:
            os.close(fd)
    return validate_next_start(root,state,role,props,files)


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
    schema_version = verify_release_tree(binary.parent, state)
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
        if (process / "cwd").resolve() != binary.parent:
            raise ValueError("canary runtime working directory is not the exact artifact release")
        raw = (process / "environ").read_bytes()
        processes[role] = dict(x.split("=", 1) for x in raw.decode().split("\0") if "=" in x)
    verify_canary_runtime(root, state, env, processes)
    api_pid = int(subprocess.check_output(["systemctl", "--user", "show",
                    "aibox126-canary-api", "-p", "MainPID", "--value"],
                    text=True, timeout=5).strip())
    if not listener_owned_by_pid(api_pid, 18082):
        raise ValueError("canary API port is not owned by verified API process")
    mapping = subprocess.run(["docker", "port", "aiboxacc107-postgres-1", "5432/tcp"],
                             capture_output=True, text=True, timeout=10)
    if mapping.returncode != 0 or mapping.stdout.strip() != "127.0.0.1:55432":
        raise ValueError("verified database endpoint is not the inspected container")
    db_user = env.get("DB_USER", "")
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", db_user):
        raise ValueError("invalid clone database role")
    check = subprocess.run(["docker", "exec", "aiboxacc107-postgres-1", "psql",
                            "-U", db_user, "-d", state["clone_db"], "-At",
                            "-v", "ON_ERROR_STOP=1", "-c",
                            "SELECT version,dirty FROM schema_migrations"],
                           capture_output=True, text=True, timeout=10)
    if check.returncode != 0 or check.stdout.strip() != str(schema_version) + "|f":
        raise ValueError("effective clone schema differs from reviewed artifact")
    return state
