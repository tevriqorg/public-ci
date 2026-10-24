#!/usr/bin/env python3
"""Bounded clone-only L3 worker restart + true indexed At-rest replay.

Never runs against old UAT or production. A passing preflight is NOT a formal
15-day soak slot. The private state guard validates exact running API/worker
binary, loopback listener, DB/Redis/storage and permissions.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

from p2_canary_guard import verify_live_canary, verify_next_start

API = "aibox126-canary-api"
WORKER = "aibox126-canary-worker"
L1 = "aibox-uat-soak126-l1-preflight"
L2 = "aibox126-canary-l2-preflight"


def systemctl(*args, timeout=18):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True,
                          text=True, timeout=timeout, check=True).stdout.strip()


def active(unit):
    # systemctl is-active returns 3 for an existing *inactive* unit. Treating
    # that as an exception makes the safe "L1/L2 not running" gate unusable.
    # Unknown units (4) and transport errors must still fail closed.
    r = subprocess.run(["systemctl", "--user", "is-active", unit + ".service"],
                       capture_output=True, text=True, timeout=10, check=False)
    if r.returncode == 0 and r.stdout.strip() == "active":
        return True
    if r.returncode == 3 and r.stdout.strip() == "inactive":
        return False
    raise ValueError("unable to verify required systemd unit status")


def main_pid(unit):
    value = systemctl("show", unit + ".service", "-p", "MainPID", "--value")
    if not value.isdecimal() or int(value) < 2:
        raise ValueError("canary worker has no valid PID")
    return int(value)


def validate_replay_proof(payload):
    if not isinstance(payload, dict):
        raise ValueError("At-rest replay did not emit JSON")
    expected = {
        "result": "PASS_CANARY_AT_REST_ONLY",
        "replays": 3, "revision_count": 1, "raw_bytes_copied": 0,
        "formal_soak_started": False,
    }
    for k, v in expected.items():
        if type(payload.get(k)) is not type(v) or payload.get(k) != v:
            raise ValueError("real indexed At-rest replay contract failed: " + k)
    if not payload.get("knowledge_id") or not payload.get("resource_ref"):
        raise ValueError("missing real WeKnora evidence identity")
    return True


def append_private(path, event):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError("L3 evidence log must be private regular file")
        os.write(fd, (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def run(root, source_root):
    # Dedicated private state must exist and be owned by this user.
    root = Path(root)
    source_root = Path(source_root)
    state = verify_live_canary(root)
    if not state["clone_db"].startswith("aibox126_canary_"):
        raise ValueError("L3 cannot run outside dedicated clone")
    script = source_root / "scripts" / "issue126_canary_at_rest.py"
    if not script.is_file() or script.is_symlink():
        raise ValueError("pinned At-rest probe missing")
    # Governed deployments must hold this same developer lease.
    devfd = os.open(root / "issue126-dev.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(devfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(devfd)
        raise
    lock = root / "l2-preflight.lock"
    fd = None
    try:
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_mode & 0o077:
            raise ValueError("unsafe L3 lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # No other preflight can be interrupted; old UAT never touched.
        if not active(API) or not active(WORKER) or active(L1) or active(L2):
            raise ValueError("another preflight active or clone not healthy")
        verify_next_start(root, state, "worker")
        before = main_pid(WORKER)
        started = time.monotonic()
        try:
            systemctl("restart", WORKER + ".service", timeout=30)
            after = main_pid(WORKER)
            if before == after:
                raise ValueError("worker restart did not replace PID")
            verified = verify_live_canary(root)
            if verified.get("source_sha") != state.get("source_sha"):
                raise ValueError("binary identity changed during worker restart")
            result = subprocess.run([sys.executable, str(script), "--state-dir", str(root)],
                                    cwd=source_root, capture_output=True, text=True,
                                    timeout=210, check=False)
            if result.returncode != 0:
                raise ValueError("indexed replay failed after canary worker restart")
            lines = result.stdout.strip().splitlines()
            if not lines:
                raise ValueError("At-rest replay missing evidence")
            proof = json.loads(lines[-1])
            validate_replay_proof(proof)
            event = {
                "result": "PRECHECK_PASS_L3_CLONE_WORKER_RESTART_ONLY",
                "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "trial_id": None, "formal_soak_started": False,
                "worker_pid_changed": True, "source_sha": state["source_sha"],
                "clone_db_checked": True, "at_rest_replays": 3,
                "source_revision_count": 1, "raw_bytes_copied": 0,
                "elapsed_seconds": round(time.monotonic() - started, 2),
            }
            append_private(root / "l3-preflight-results.jsonl", event)
            return event
        finally:
            # Even a failed probe must leave the canary worker running.
            if not active(WORKER):
                systemctl("start", WORKER + ".service", timeout=30)
    finally:
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        fcntl.flock(devfd, fcntl.LOCK_UN)
        os.close(devfd)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--state-dir", type=Path, required=True)
    p.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    a = p.parse_args()
    try:
        proof = run(a.state_dir, a.source_root)
        print(json.dumps({k: proof[k] for k in
                          ("result", "worker_pid_changed", "at_rest_replays",
                           "source_revision_count", "formal_soak_started")}))
    except Exception as exc:
        # Never print tokens, source contents or DB connection strings.
        print(json.dumps({"result": "PRECHECK_BLOCKED_L3_CLONE_ONLY",
                          "error_type": type(exc).__name__,
                          "formal_soak_started": False}))
        sys.exit(2)


if __name__ == "__main__":
    main()
