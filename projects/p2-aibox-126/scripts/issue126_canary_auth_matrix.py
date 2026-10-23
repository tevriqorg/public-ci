"""#126 live clone-only scoped API-key/KB boundary matrix.

No valid write payload is ever submitted. Invalid envelopes/descriptors
must be rejected even if a route mistakenly permits access. Does not
create, delete or update Knowledge, Resource or KB state.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import uuid
from urllib.error import HTTPError
from urllib.request import Request, HTTPRedirectHandler, build_opener

from p2_canary_guard import verify_live_canary, secure_canary_read, parse_private_env


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def query_other_kb(root: Path, own: str, state: dict) -> str:
    uuid.UUID(own)
    env = parse_private_env(secure_canary_read(root, root / "canary.env"))
    if env.get("DB_NAME") != state["clone_db"]:
        raise ValueError("clone DB changed")
    sql = "SELECT id FROM knowledge_bases WHERE id <> '" + own + "' LIMIT 1"
    out = subprocess.run(
        ["docker", "exec", "aiboxacc107-postgres-1", "psql",
         "-U", env["DB_USER"], "-d", state["clone_db"],
         "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True, timeout=12, check=True)
    other = out.stdout.strip()
    uuid.UUID(other)
    if other == own:
        raise ValueError("independent KB required")
    return other


def run(root: Path):
    state = verify_live_canary(root)
    own = json.loads(secure_canary_read(root, root / "test-kb.json"))["kb_id"]
    uuid.UUID(own)
    other = query_other_kb(root, own, state)
    read_key = secure_canary_read(root, root / "read-key.token").strip()
    ingest_key = secure_canary_read(root, root / "ingest-key.token").strip()
    if len(read_key) < 16 or len(ingest_key) < 16 or read_key == ingest_key:
        raise ValueError("invalid independent scoped credentials")
    opener = build_opener(NoRedirect())
    invalid_descriptor = {
        "resource_path": "local://10000/aibox126/invalid.txt",
        "name": "invalid.txt", "mime": "text/plain",
        "sha256": "z" * 64, "size": 1}
    # A missing derived envelope is invalid even if authorization regresses.
    cases = [
        ("read-own", "GET", f"/knowledge-bases/{own}", read_key, None, 200),
        ("ingest-own", "GET", f"/knowledge-bases/{own}", ingest_key, None, 200),
        ("read-other", "GET", f"/knowledge-bases/{other}", read_key, None, 403),
        ("ingest-other", "GET", f"/knowledge-bases/{other}", ingest_key, None, 403),
        ("read-cannot-register", "POST", f"/knowledge-bases/{own}/knowledge/external-resource", read_key, invalid_descriptor, 403),
        ("ingest-invalid-descriptor", "POST", f"/knowledge-bases/{own}/knowledge/external-resource", ingest_key, invalid_descriptor, 400),
        ("ingest-cannot-write-other", "POST", f"/knowledge-bases/{other}/knowledge/external-resource", ingest_key, invalid_descriptor, 403),
        ("ingest-cannot-derive-other", "POST", f"/knowledge-bases/{other}/knowledge/derived", ingest_key, {}, 403),
        ("ingest-invalid-derived", "POST", f"/knowledge-bases/{own}/knowledge/derived", ingest_key, {}, 400),
        ("read-cannot-search-other", "POST", f"/knowledge-bases/{other}/hybrid-search", read_key, {"query_text": "public canary", "match_count": 1}, 403),
    ]
    results = []
    for name, method, route, key, payload, expected in cases:
        req = Request("http://127.0.0.1:18082/api/v1" + route,
                      data=None if payload is None else json.dumps(payload).encode(),
                      headers={"X-API-Key": key, "Content-Type": "application/json"},
                      method=method)
        try:
            with opener.open(req, timeout=12) as response:
                status = response.status
                response.read(65537)
        except HTTPError as e:
            status = e.code
            e.close()
        if status != expected:
            raise AssertionError(f"{name}: expected HTTP {expected}, got {status}")
        results.append(name)
    proof = {
        "result": "PRECHECK_PASS_CLONE_KB_SCOPE_ONLY",
        "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "case_count": len(results),
        "passed_cases": results,
        "other_kb_is_clone_only": True,
        "cross_tenant_live_tested": False,
        "resource_cross_kb_ref_tested": False,
        "valid_writes_attempted": 0,
        "formal_soak_started": False,
    }
    out = root / "auth-matrix-canary-result.json"
    out.write_text(json.dumps(proof, indent=2))
    out.chmod(0o600)
    print(json.dumps(proof))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.state_dir)
    except Exception as exc:
        print(json.dumps({"result": "FAIL_CANARY_AUTH_MATRIX",
                          "error_type": type(exc).__name__,
                          "reason": str(exc)[:120] if isinstance(exc, AssertionError) else "see private runtime diagnosis"}))
        sys.exit(1)
