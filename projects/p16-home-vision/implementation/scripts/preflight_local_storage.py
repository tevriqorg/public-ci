from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

NETWORK_FILESYSTEMS = {
    "nfs",
    "nfs4",
    "cifs",
    "smb3",
    "smbfs",
    "fuse.sshfs",
    "9p",
    "ceph",
    "glusterfs",
}


class PreflightError(RuntimeError):
    pass


@dataclass(frozen=True)
class PathCheck:
    role: str
    ok: bool
    filesystem_type: str | None = None
    writable_smoke: bool = False
    sqlite_wal_smoke: bool = False
    device_id: int | None = None


@dataclass(frozen=True)
class PreflightResult:
    verdict: str
    state: PathCheck
    archive_a: PathCheck
    archive_b: PathCheck
    distinct_archive_paths: bool
    distinct_archive_devices: bool


def _require_directory(path: Path, role: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise PreflightError(f"{role}_directory_missing")
    return resolved


def _filesystem_type(path: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["findmnt", "-T", str(path), "-n", "-o", "FSTYPE"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip().lower()
    return value or None


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def writable_smoke(path: Path, role: str) -> None:
    token = uuid.uuid4().hex
    initial = path / f".home-vision-preflight-{role}-{token}.tmp"
    renamed = path / f".home-vision-preflight-{role}-{token}.renamed"
    payload = b"home-vision-preflight\n"
    fd: int | None = None
    try:
        fd = os.open(initial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(initial, renamed)
        _fsync_directory(path)
        if renamed.read_bytes() != payload:
            raise PreflightError(f"{role}_smoke_content_mismatch")
        renamed.unlink()
        _fsync_directory(path)
    finally:
        if fd is not None:
            os.close(fd)
        for candidate in (initial, renamed):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass


def sqlite_wal_smoke(path: Path) -> None:
    token = uuid.uuid4().hex
    database = path / f".home-vision-preflight-state-{token}.sqlite3"
    sidecars = [Path(f"{database}-wal"), Path(f"{database}-shm")]
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database)
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise PreflightError("state_sqlite_wal_unavailable")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO probe(value) VALUES ('ok')")
        connection.commit()
        value = connection.execute("SELECT value FROM probe WHERE id=1").fetchone()[0]
        if value != "ok":
            raise PreflightError("state_sqlite_roundtrip_failed")
    finally:
        if connection is not None:
            connection.close()
        for candidate in [database, *sidecars]:
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass


def _check_state(path: Path) -> PathCheck:
    filesystem_type = _filesystem_type(path)
    if filesystem_type in NETWORK_FILESYSTEMS:
        raise PreflightError(f"state_network_filesystem_rejected:{filesystem_type}")
    writable_smoke(path, "state")
    sqlite_wal_smoke(path)
    stat = path.stat()
    return PathCheck(
        role="state",
        ok=True,
        filesystem_type=filesystem_type,
        writable_smoke=True,
        sqlite_wal_smoke=True,
        device_id=stat.st_dev,
    )


def _check_archive(path: Path, role: str) -> PathCheck:
    writable_smoke(path, role)
    stat = path.stat()
    return PathCheck(
        role=role,
        ok=True,
        filesystem_type=_filesystem_type(path),
        writable_smoke=True,
        device_id=stat.st_dev,
    )


def run_preflight(
    state_dir: Path,
    archive_a: Path,
    archive_b: Path,
    *,
    require_distinct_devices: bool = False,
) -> PreflightResult:
    state = _require_directory(state_dir, "state")
    a = _require_directory(archive_a, "archive_a")
    b = _require_directory(archive_b, "archive_b")

    if state in {a, b}:
        raise PreflightError("state_must_not_share_archive_directory")
    if a == b:
        raise PreflightError("archive_a_and_b_must_be_distinct_paths")

    state_check = _check_state(state)
    a_check = _check_archive(a, "archive_a")
    b_check = _check_archive(b, "archive_b")
    distinct_devices = a_check.device_id != b_check.device_id
    if require_distinct_devices and not distinct_devices:
        raise PreflightError("archive_a_and_b_not_distinct_devices")

    return PreflightResult(
        verdict="PASS",
        state=state_check,
        archive_a=a_check,
        archive_b=b_check,
        distinct_archive_paths=True,
        distinct_archive_devices=distinct_devices,
    )


def _public_payload(result: PreflightResult) -> dict[str, object]:
    payload = asdict(result)
    # Device ids are useful only for the local decision and should not be carried
    # into durable evidence. Keep the report role-based and redacted.
    for key in ("state", "archive_a", "archive_b"):
        payload[key].pop("device_id", None)  # type: ignore[union-attr]
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Home Vision local state/archive preflight")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--archive-a", type=Path, required=True)
    parser.add_argument("--archive-b", type=Path, required=True)
    parser.add_argument("--require-distinct-devices", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        result = run_preflight(
            args.state_dir,
            args.archive_a,
            args.archive_b,
            require_distinct_devices=args.require_distinct_devices,
        )
    except PreflightError as exc:
        payload = {"verdict": "BLOCKED", "reason": str(exc)}
        print(json.dumps(payload, ensure_ascii=False) if args.json else f"BLOCKED: {exc}")
        return 2

    payload = _public_payload(result)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True) if args.json else "PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
