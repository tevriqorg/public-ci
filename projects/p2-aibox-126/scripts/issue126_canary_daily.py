#!/usr/bin/env python3
"""Daily *preflight* E2E subset, not a formal 15-day soak or Gate 0 signature.

Executes only clone-bound, idempotent business probes. Never invokes outage,
reprovision, backup restore, destructive cleanup or private data export.
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

from p2_canary_guard import verify_live_canary
from issue126_canary_session_renew import ensure_fresh_session

CASES = (
    ("at_rest", "issue126_canary_at_rest.py", "PASS_CANARY_AT_REST_ONLY"),
    ("transit", "issue126_canary_transit.py", "PASS_CANARY_TRANSIT_HANDOFF_ONLY"),
    ("continuous", "issue126_canary_continuous.py", "PASS_CANARY_CONTINUOUS_BACKFILL_REPLAY_ONLY"),
    ("scoped_auth", "issue126_canary_auth_matrix.py", "PRECHECK_PASS_CLONE_KB_SCOPE_ONLY"),
)
UNTESTED = ("real_update", "explicit_withdrawal", "cross_tenant",
            "cross_kb_resource_reference", "media_oracle", "temporal",
            "clone_fault_injection", "repair_agent_handoff")


def _append_private(path: Path, event: dict):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError("unsafe private L2 log")
        os.write(fd, (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def evaluate_checks(checks: list[dict]):
    """Do not count script exit=0 or a generic PASS as actual business proof."""
    if len(checks) != len(CASES):
        return False
    by_name = {c.get("name"): c for c in checks}
    if set(by_name) != {name for name, _, _ in CASES}:
        return False
    for name, _, verdict in CASES:
        x = by_name[name]
        if x.get("exit_code") != 0 or x.get("result") != verdict:
            return False
    return (by_name["at_rest"].get("replays") == 3
            and by_name["at_rest"].get("revision_count") == 1
            and by_name["at_rest"].get("raw_bytes_copied") == 0
            and by_name["transit"].get("state") == "complete"
            and by_name["transit"].get("staging_cleaned") is True
            and by_name["continuous"].get("cursor_persisted") is True
            and by_name["continuous"].get("real_search_hit") is True
            and by_name["scoped_auth"].get("case_count") >= 10
            and all(x.get("formal_soak_started") is False for x in by_name.values()))


def run(root: Path, *, source_root: Path):
    start = time.monotonic()
    # Guard checks effective API/worker/DB/Redis/storage, binary digest and listener.
    state = verify_live_canary(root)
    lock = root / "l2-preflight.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_mode & 0o077:
            raise ValueError("unsafe L2 lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another L2 preflight owns the exclusive lock") from exc
        # The dedicated clone session JWT lasts only 24h; fail closed and
        # rotate before its expiry, without changing any production credentials.
        # This maintenance probe is NOT a fifth business acceptance case.
        try:
            session_renew = ensure_fresh_session(root, guard=lambda _: state)
        except Exception as exc:
            _append_private(root / "l2-preflight-results.jsonl", {
                "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "result": "PRECHECK_BLOCKED_L2_SUBSET", "trial_id": None,
                "formal_soak_started": False, "checks": [],
                "session_renew_error_type": type(exc).__name__,
                "source_revision": "session-renew-failed",
            })
            raise
        checks = []
        for name, filename, _ in CASES:
            script = source_root / "scripts" / filename
            if not script.is_file() or script.is_symlink():
                raise ValueError("pinned canary script missing")
            try:
                completed = subprocess.run(
                    [sys.executable, str(script), "--state-dir", str(root)],
                    cwd=source_root, capture_output=True, text=True, timeout=240,
                    check=False)
                last = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""
                payload = json.loads(last)
                if not isinstance(payload, dict):
                    raise ValueError("invalid probe output")
                # Whitelist evidence fields; never persist raw response/secret/error body.
                keep = {"result", "replays", "revision_count", "raw_bytes_copied",
                        "state", "staging_cleaned", "cursor_persisted", "real_search_hit",
                        "case_count", "formal_soak_started"}
                entry = {"name": name, "exit_code": completed.returncode}
                entry.update({k: payload[k] for k in keep if k in payload})
            except (subprocess.TimeoutExpired, ValueError, json.JSONDecodeError, IndexError) as e:
                entry = {"name": name, "exit_code": -1, "error_type": type(e).__name__}
            checks.append(entry)
            if entry.get("exit_code") != 0:
                break
        ok = evaluate_checks(checks)
        event = {
            "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "result": "PRECHECK_PASS_L2_SUBSET" if ok else "PRECHECK_FAIL_L2_SUBSET",
            "trial_id": None, "formal_soak_started": False,
            "checks": checks, "tested_cases": len(checks),
            "missing_gate0_capabilities": list(UNTESTED),
            "source_revision": (source_root / "SOURCE_REVISION").read_text().strip()
                if (source_root / "SOURCE_REVISION").is_file() else "unfrozen-source",
            "canary_source_sha": state.get("source_sha"),
            "duration_seconds": round(time.monotonic() - start, 2),
            "session_renewed": session_renew["rotated"],
            "session_access_expires_at_utc": session_renew["access_expires_at_utc"],
        }
        _append_private(root / "l2-preflight-results.jsonl", event)
        print(json.dumps({"result": event["result"], "tested_cases": len(checks),
                          "source_revision": event["source_revision"],
                          "formal_soak_started": False}))
        return 0 if ok else 2
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--state-dir", type=Path, required=True)
    p.add_argument("--source-root", type=Path,
                   default=Path(__file__).resolve().parents[1])
    args = p.parse_args()
    try:
        sys.exit(run(args.state_dir, source_root=args.source_root))
    except Exception as exc:
        # Deliberately avoid private path/credential/raw response in output.
        print(json.dumps({"result": "PRECHECK_BLOCKED_L2_SUBSET",
                          "error_type": type(exc).__name__,
                          "formal_soak_started": False}))
        sys.exit(2)


if __name__ == "__main__":
    main()
