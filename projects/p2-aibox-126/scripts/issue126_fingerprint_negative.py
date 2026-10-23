#!/usr/bin/env python3
"""#126 exact-artifact live fingerprint negative, isolated clone only.

Not scheduled as an L1/L2/L3 preflight: the old canary is expected to FAIL.
No private state is shipped to public CI. Never writes to original UAT.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from p2_canary_guard import verify_live_canary, secure_canary_read, parse_private_env

PATH = "local://10000/aibox126/rfc9110.txt"
PG_CONTAINER = "aiboxacc107-postgres-1"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def mismatched_sha256(source_sha: str) -> str:
    if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha):
        raise ValueError("invalid frozen SHA-256")
    return ("0" if source_sha[0] != "0" else "1") + source_sha[1:]


def rejection_verified(status: int, response: dict) -> bool:
    """A successful/redirected/auth-failed response cannot pass this regression."""
    return status == 400 and isinstance(response, dict) and response.get("success") is False


def resource_snapshot(db: str, user: str) -> tuple[str, int, str, int]:
    if not re.fullmatch(r"aibox126_canary_[a-z0-9_]+", db):
        raise ValueError("not the dedicated clone database")
    sql = ("SELECT COALESCE(json_agg(row_to_json(r)), '[]') FROM "
           "(SELECT * FROM resources WHERE physical_path='" + PATH +
           "' AND deleted_at IS NULL ORDER BY id) r")
    p = subprocess.run(["docker", "exec", PG_CONTAINER, "psql", "-U", user,
                        "-d", db, "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
                       capture_output=True, text=True, timeout=15, check=True)
    rows = json.loads(p.stdout)
    if len(rows) != 1 or rows[0].get("ownership") != "external":
        raise ValueError("expected exactly one existing external clone resource")
    row = rows[0]
    return (hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            len(rows), row["content_hash"], int(row["size"]))


def probe(root: Path) -> dict:
    state = verify_live_canary(root)
    oracle = json.loads(secure_canary_read(root, root / "at-rest-oracle-v1.json"))
    kb = oracle["kb_id"]
    uuid.UUID(kb)
    raw = Path("/home/ubuntu/runtime/aibox-0.8-uat/real-corpus/rfc9110.txt")
    alias = root / "storage/10000/aibox126/rfc9110.txt"
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    if (digest != oracle["source_sha256"] or
            raw.stat().st_mtime_ns != oracle["source_mtime_ns"] or
            not os.path.samefile(raw, alias)):
        raise ValueError("frozen Raw oracle mismatch")
    env = parse_private_env(secure_canary_read(root, root / "canary.env"))
    if env.get("DB_NAME") != state["clone_db"]:
        raise ValueError("clone database mismatch")
    before = resource_snapshot(state["clone_db"], env["DB_USER"])
    if before[2] != digest or before[3] != raw.stat().st_size:
        raise ValueError("existing resource fingerprint does not match frozen Raw")
    key = secure_canary_read(root, root / "ingest-key.token").strip()
    if len(key) < 16:
        raise ValueError("invalid dedicated ingest key")
    descriptor = {"resource_path": PATH, "size": before[3],
                  "sha256": mismatched_sha256(digest),
                  "name": "rfc9110.txt", "mime": "text/plain"}
    request = Request("http://127.0.0.1:18082/api/v1/knowledge-bases/" +
                      kb + "/knowledge/external-resource",
                      data=json.dumps(descriptor).encode(),
                      headers={"X-API-Key": key, "Content-Type": "application/json"},
                      method="POST")
    opener = build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            status = response.status
            body = json.loads(response.read(65536))
    except HTTPError as exc:
        status = exc.code
        try:
            body = json.loads(exc.read(65536))
        except (ValueError, UnicodeDecodeError):
            body = {}
        finally:
            exc.close()
    after = resource_snapshot(state["clone_db"], env["DB_USER"])
    raw_ok = (hashlib.sha256(raw.read_bytes()).hexdigest() == digest and
              raw.stat().st_mtime_ns == oracle["source_mtime_ns"])
    unchanged = before == after and raw_ok
    passed = rejection_verified(status, body) and unchanged
    result = {"result": "PRECHECK_PASS_FINGERPRINT_REJECTION_ONLY" if passed else "FAIL_FINGERPRINT_REJECTION",
              "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "http_status": status, "server_success": body.get("success"),
              "resource_row_unchanged": before == after, "raw_unchanged": raw_ok,
              "frozen_source_sha256": digest,
              "canary_binary_source": state.get("source_sha"),
              "formal_soak_started": False, "trial_id": None}
    evidence = root / "issue126-evidence"
    if not evidence.is_dir() or evidence.is_symlink():
        raise ValueError("private evidence directory unavailable")
    target = evidence / ("fingerprint-negative-" + uuid.uuid4().hex + ".json")
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as sink:
        json.dump(result, sink, sort_keys=True)
        sink.flush()
        os.fsync(sink.fileno())
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.state_dir.is_dir() or not (args.state_dir / "canary.env").is_file():
        print(json.dumps({"result": "PRECHECK_BLOCKED_FINGERPRINT", "formal_soak_started": False}))
        return 2
    try:
        result = probe(args.state_dir)
    except Exception as exc:
        print(json.dumps({"result": "FAIL_FINGERPRINT_PROBE", "error_type": type(exc).__name__,
                          "formal_soak_started": False}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["result"].startswith("PRECHECK_PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
