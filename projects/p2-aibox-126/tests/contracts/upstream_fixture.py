from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = ROOT / "deploy" / "upstreams.lock.yaml"


def _strip_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
        return value[1:-1]
    if value == "true":
        return "true"
    if value == "false":
        return "false"
    return value


def load_lock_scalars() -> dict[str, str]:
    """Parse the mapping/scalar subset used by upstreams.lock.yaml without PyYAML."""
    result: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    for raw in LOCK_PATH.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if line.startswith("-") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = ".".join([entry[1] for entry in stack] + [key])
        value = value.strip()
        if value:
            result[path] = _strip_scalar(value)
        else:
            stack.append((indent, key))
    return result


LOCK = load_lock_scalars()


def lock(path: str) -> str:
    try:
        return LOCK[path]
    except KeyError as exc:
        raise AssertionError(f"missing canonical lock value: {path}") from exc


def run(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        args,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        command = " ".join(args)
        raise AssertionError(
            f"command failed ({proc.returncode}): {command}\n{proc.stdout}\n{proc.stderr}"
        )
    return proc.stdout.strip()


def _cache_root() -> Path:
    """Use a private per-UID checkout, never a shared mutable Git worktree.

    A stale global /tmp/aibox-g0-upstream-contracts from another user can
    cause git dubious-ownership failures or allow cross-user source injection.
    Do not silently chmod, delete or trust someone else's cache.
    """
    configured = os.environ.get("AIBOX_UPSTREAM_CACHE")
    if configured:
        root = Path(configured).expanduser()
    else:
        root = Path(tempfile.gettempdir()) / f"aibox-g0-upstream-contracts-{os.geteuid()}"
    if root.is_symlink():
        raise ValueError("upstream cache root must not be a symlink")
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    st = root.stat()
    if st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise ValueError("upstream cache must be owned by current user and private")
    return root.resolve()


def ensure_checkout(name: str, repository: str, commit: str) -> Path:
    root = _cache_root()
    checkout = root / name
    if not (checkout / ".git").exists():
        if checkout.exists():
            shutil.rmtree(checkout)
        checkout.mkdir(parents=True)
        run("git", "init", "-q", str(checkout))
        run("git", "-C", str(checkout), "remote", "add", "origin", repository)
    try:
        run("git", "-C", str(checkout), "cat-file", "-e", f"{commit}^{{commit}}")
    except AssertionError:
        run("git", "-C", str(checkout), "fetch", "-q", "--depth=1", "origin", commit)
    run("git", "-C", str(checkout), "checkout", "-q", "--detach", commit)
    actual = run("git", "-C", str(checkout), "rev-parse", "HEAD")
    if actual != commit:
        raise AssertionError(f"{name} checkout drift: lock={commit}, actual={actual}")
    return checkout


def upstream_checkouts() -> tuple[Path, Path]:
    weknora = ensure_checkout(
        "WeKnora",
        lock("upstreams.weknora.repository"),
        lock("upstreams.weknora.commit"),
    )
    graphiti = ensure_checkout(
        "graphiti",
        lock("upstreams.graphiti.repository"),
        lock("upstreams.graphiti.commit"),
    )
    return weknora, graphiti


def text(root: Path, relative: str) -> str:
    path = root / relative
    if not path.is_file():
        raise AssertionError(f"missing pinned upstream file: {relative}")
    return path.read_text(encoding="utf-8")


def section(source: str, start_pattern: str, end_pattern: str | None = None) -> str:
    start = re.search(start_pattern, source, re.MULTILINE)
    if not start:
        raise AssertionError(f"section start not found: {start_pattern}")
    tail = source[start.start():]
    if end_pattern:
        end = re.search(end_pattern, tail[len(start.group(0)):], re.MULTILINE)
        if end:
            return tail[: len(start.group(0)) + end.start()]
    return tail
