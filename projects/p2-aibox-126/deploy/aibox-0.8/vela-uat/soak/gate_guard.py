"""Fail-closed trial authorization; a bare timestamp NEVER starts a 15-day soak.

The approval key is runtime-private, outside Git. This verifier cannot create a
trial or mark acceptance; an independent reviewer signs the frozen evidence.
"""
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path

FROZEN_MATRIX_SHA256 = "c3b3d89b96f1008e171831780b447bbc8d56e1f218d4651236ad7b9fb962b2e1"
REQUIRED = (
    "schema", "trial_id", "start_utc", "matrix_sha256", "oracle_sha256",
    "p2_sha", "weknora_sha", "runtime_digest", "rollback_snapshot",
    "approval_evidence_sha256", "at_rest_receipt_sha256",
    "transit_receipt_sha256", "continuous_receipt_sha256",
)
HEX = set("0123456789abcdef")


def canonical_payload(d):
    return json.dumps({k: v for k, v in d.items() if k != "signature_hmac_sha256"},
                      separators=(",", ":"), sort_keys=True).encode("utf-8")


def validate_gate(path: Path, key_path: Path | None = None, now=None):
    """Return (gate or None, reason), rejecting missing/unsigned/tampered proof."""
    if not path.is_file():
        return None, "preflight: gate not installed"
    try:
        gate = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(gate, dict) or any(not gate.get(k) for k in REQUIRED):
            raise ValueError("missing required gate evidence")
        if gate["schema"] != "p2-soak-gate-v1":
            raise ValueError("wrong gate schema")
        if gate["matrix_sha256"] != FROZEN_MATRIX_SHA256:
            raise ValueError("baseline test matrix changed")
        if any(not isinstance(gate.get(k), str) for k in REQUIRED):
            raise ValueError("invalid gate field")
        for k in ("oracle_sha256", "approval_evidence_sha256", "at_rest_receipt_sha256",
                  "transit_receipt_sha256", "continuous_receipt_sha256"):
            v = gate[k]
            if len(v) != 64 or any(c not in HEX for c in v):
                raise ValueError("invalid SHA-256 evidence")
        for k in ("p2_sha", "weknora_sha"):
            v = gate[k]
            if len(v) != 40 or any(c not in HEX for c in v):
                raise ValueError("invalid exact commit SHA")
        key_path = key_path or Path(os.environ.get("P2_SOAK_GATE_KEY_FILE", "/nonexistent"))
        if not key_path.is_file() or (key_path.stat().st_mode & 0o077):
            raise ValueError("independent gate approval key unavailable or insecure")
        key = key_path.read_bytes()
        if len(key) < 32:
            raise ValueError("weak approval key")
        sig = gate.get("signature_hmac_sha256", "")
        expected = hmac.new(key, canonical_payload(gate), hashlib.sha256).hexdigest()
        if not isinstance(sig, str) or not hmac.compare_digest(sig, expected):
            raise ValueError("unsigned or tampered gate")
        start = dt.datetime.fromisoformat(gate["start_utc"].replace("Z", "+00:00"))
        now = now or dt.datetime.now(dt.timezone.utc)
        if start.tzinfo is None or start > now or (now-start).total_seconds() < 0:
            raise ValueError("invalid UTC start")
        if not gate["trial_id"].startswith("P2-126-"):
            raise ValueError("trial identity missing")
        return gate, "approved"
    except (ValueError, TypeError, OSError, json.JSONDecodeError) as e:
        return None, "invalid gate: " + str(e)
