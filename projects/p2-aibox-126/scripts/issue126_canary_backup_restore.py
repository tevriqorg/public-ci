#!/usr/bin/env python3
"""#126 isolated clone PostgreSQL backup and *actual* restore smoke.

Never connects to or changes the original UAT database. Uses a fresh uniquely
named disposable DB created from template0 and drops only that exact name.
Does not promote a preflight into a formal 15-day soak acceptance.
"""
from __future__ import annotations
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

from p2_canary_guard import verify_live_canary, secure_canary_read, parse_private_env
from issue126_canary_l3_worker_restart import active

PG_CONTAINER = "aiboxacc107-postgres-1"
TABLES = ("knowledge_bases", "knowledges", "resources", "source_revisions")
MAX_DUMP_BYTES = 200 * 1024 * 1024


def safe_database(name: str, *, temporary: bool = False):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", name):
        raise ValueError("unsafe database identifier")
    if temporary:
        if not re.fullmatch(r"aibox126_restoreprobe_[0-9a-f]{12}", name):
            raise ValueError("not a dedicated disposable restore database")
    elif not name.startswith("aibox126_canary_"):
        raise ValueError("not a dedicated clone database")
    return name


def checked(cmd, *, timeout=35, stdin=None):
    result = subprocess.run(cmd, stdin=stdin, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError("clone-only PostgreSQL operation failed")
    return result.stdout


def counts(db, user):
    sql = "SELECT " + ",".join("(SELECT count(*) FROM " + t + ")" for t in TABLES)
    raw = checked(["docker", "exec", PG_CONTAINER, "psql", "-U", user,
                   "-d", db, "-At", "-v", "ON_ERROR_STOP=1", "-c", sql])
    values = raw.decode("ascii").strip().split("|")
    if len(values) != len(TABLES) or not all(v.isdecimal() for v in values):
        raise ValueError("invalid independent database count evidence")
    return dict(zip(TABLES, map(int, values)))


def table_digests(db, user):
    """Deterministic full-row digest, not merely table counts or dump bytes."""
    result = {}
    for table in TABLES:
        sql = ("SELECT md5(COALESCE(string_agg(to_jsonb(t)::text, "
               "E'\\n' ORDER BY to_jsonb(t)::text), '')) FROM "
               + table + " t")
        raw = checked(["docker", "exec", PG_CONTAINER, "psql", "-U", user,
                       "-d", db, "-At", "-v", "ON_ERROR_STOP=1", "-c", sql])
        value = raw.decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise ValueError("invalid full-row content digest")
        result[table] = value
    return result


def validate_restore(source, restored, before_dump, *,
                     source_digest, restored_digest, before_digest):
    if source != before_dump or restored != source:
        raise ValueError("clone backup/restore row-count consistency failed")
    if not source["knowledges"] or not source["source_revisions"]:
        raise ValueError("restore oracle has no real indexed source/revision")
    if (set(source_digest) != set(TABLES)
            or source_digest != before_digest or restored_digest != source_digest):
        raise ValueError("clone backup/restore full-row content mismatch")
    if not all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{32}", v)
               for v in source_digest.values()):
        raise ValueError("invalid full-row digest")
    return True


def dump_clone(db, user, target: Path):
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    hasher = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as sink:
            process = subprocess.Popen(
                ["docker", "exec", PG_CONTAINER, "pg_dump", "-U", user, "-d", db,
                 "--format=custom", "--no-owner", "--no-privileges"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                while chunk := process.stdout.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_DUMP_BYTES:
                        raise ValueError("backup exceeds bounded clone-only cap")
                    sink.write(chunk)
                    hasher.update(chunk)
                if process.wait(timeout=120) != 0:
                    raise RuntimeError("clone pg_dump failed")
                sink.flush()
                os.fsync(sink.fileno())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=8)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    if size < 1024:
        target.unlink(missing_ok=True)
        raise ValueError("unexpectedly empty clone backup")
    return size, hasher.hexdigest()


def run(root: Path):
    root = root.resolve(strict=True)
    state = verify_live_canary(root)
    clone = safe_database(state["clone_db"])
    if clone == state["original_db"]:
        raise ValueError("original UAT database is not a clone")
    private = root / "canary.env"
    env = parse_private_env(secure_canary_read(root, private))
    if env.get("DB_NAME") != clone:
        raise ValueError("canary effective DB mismatch")
    user = env.get("DB_USER", "")
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", user):
        raise ValueError("invalid database role")
    lock = root / "l2-preflight.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        lockstat = os.fstat(fd)
        if not stat.S_ISREG(lockstat.st_mode) or lockstat.st_mode & 0o077:
            raise ValueError("unsafe shared preflight lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (not active("aibox126-canary-api")
                or not active("aibox126-canary-worker")
                or active("aibox-uat-soak126-l1-preflight")
                or active("aibox126-canary-l2-preflight")
                or active("aibox126-canary-l3-preflight")):
            raise ValueError("clone service not healthy or preflight busy")
        before = counts(clone, user)
        before_digest = table_digests(clone, user)
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = root / "issue126-evidence" / "backup-restore"
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        if backup_dir.is_symlink() or backup_dir.stat().st_mode & 0o077:
            raise ValueError("backup directory is not private")
        backup = backup_dir / ("clone-" + now + "-" + uuid.uuid4().hex[:8] + ".dump")
        disposable = safe_database("aibox126_restoreprobe_" + uuid.uuid4().hex[:12], temporary=True)
        created = False
        cleanup_ok = False
        start = time.monotonic()
        try:
            size, digest = dump_clone(clone, user, backup)
            with backup.open("rb") as src:
                toc = checked(["docker", "exec", "-i", PG_CONTAINER,
                               "pg_restore", "--list"], stdin=src)
            if not all(("TABLE public " + name).encode() in toc for name in TABLES):
                raise ValueError("backup TOC is missing required clone tables")
            if counts(clone, user) != before or table_digests(clone, user) != before_digest:
                raise ValueError("clone changed during backup; cannot freeze oracle")
            checked(["docker", "exec", PG_CONTAINER, "createdb", "-U", user,
                     "-T", "template0", disposable], timeout=25)
            created = True
            with backup.open("rb") as src:
                checked(["docker", "exec", "-i", PG_CONTAINER, "pg_restore",
                         "-U", user, "-d", disposable, "--no-owner",
                         "--no-privileges", "--exit-on-error"], timeout=160, stdin=src)
            after = counts(disposable, user)
            source_digest = table_digests(clone, user)
            restored_digest = table_digests(disposable, user)
            validate_restore(before, after, counts(clone, user),
                             source_digest=source_digest,
                             restored_digest=restored_digest,
                             before_digest=before_digest)
            proof = {
                "result": "PRECHECK_PASS_CLONE_BACKUP_RESTORE_ONLY",
                "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "trial_id": None, "formal_soak_started": False,
                "source_sha": state["source_sha"], "clone_only": True,
                "dump_sha256": digest, "dump_bytes": size,
                "toc_required_tables": list(TABLES),
                "restored_counts": after, "row_counts_match": True,
                "full_row_content_match": True,
                "source_row_digests": source_digest,
                "elapsed_seconds": round(time.monotonic() - start, 2),
                "disposable_database_removed": False,
            }
        finally:
            if created:
                checked(["docker", "exec", PG_CONTAINER, "dropdb", "-U", user,
                         "--if-exists", disposable], timeout=25)
                cleanup_ok = True
        if not cleanup_ok:
            raise ValueError("disposable restore database cleanup not confirmed")
        proof["disposable_database_removed"] = True
        # Preserve the private binary backup and immutable hash for later audit.
        evidence = backup.with_suffix(".proof.json")
        evidence.write_text(json.dumps(proof, sort_keys=True, indent=2))
        evidence.chmod(0o600)
        print(json.dumps(proof, sort_keys=True))
        return proof
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.state_dir)
    except Exception as exc:
        print(json.dumps({"result": "PRECHECK_BLOCKED_CLONE_BACKUP_RESTORE",
                          "error_type": type(exc).__name__,
                          "formal_soak_started": False}))
        sys.exit(2)
