"""P2 #126 public synthetic L1 signed-oracle and preflight exclusion regression."""
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy/aibox-0.8/vela-uat/soak"))
from gate_guard import FROZEN_MATRIX_SHA256, canonical_payload
from soak_engine import append_event, evaluate
from business_probe import run as business_run

class L1SoakEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.key = self.root / "approval.key"
        self.key.write_bytes(os.urandom(40))
        self.key.chmod(0o600)
        self.start = dt.datetime(2026, 10, 20, tzinfo=dt.timezone.utc)
        self.gate = {
            "schema": "p2-soak-gate-v1", "trial_id": "P2-126-public-l1",
            "start_utc": self.start.isoformat(), "matrix_sha256": FROZEN_MATRIX_SHA256,
            "oracle_sha256": "a"*64, "p2_sha": "b"*40, "weknora_sha": "c"*40,
            "runtime_digest": "sha256:"+"d"*64, "rollback_snapshot": "synthetic",
            "approval_evidence_sha256": "e"*64,
            "at_rest_receipt_sha256": "1"*64, "transit_receipt_sha256": "2"*64,
            "continuous_receipt_sha256": "3"*64,
        }

    def sign(self):
        gate = dict(self.gate)
        gate["signature_hmac_sha256"] = hmac.new(
            self.key.read_bytes(), canonical_payload(gate), hashlib.sha256
        ).hexdigest()
        p = self.root / "gate.json"
        p.write_text(json.dumps(gate))
        p.chmod(0o600)

    def test_preflight_and_failed_records_cannot_fill_l1(self):
        self.sign()
        for mode, result in (("preflight","PASS"),("soak","FAIL"),(None,"PASS")):
            append_event(self.root / "business.jsonl", {
                "timestamp_utc": (self.start+dt.timedelta(minutes=20)).isoformat(),
                "mode": mode, "trial_id": self.gate["trial_id"],
                "case_id": "KB01", "result": result,
                "oracle_sha256": self.gate["oracle_sha256"]})
        out = evaluate(self.root, now=self.start+dt.timedelta(hours=1), gate_key=self.key)
        self.assertEqual(out["l1_slots"], 0)

    def test_signed_l1_profile_bound_to_frozen_oracle_and_tamper_rejected(self):
        kb, kid = str(uuid.uuid4()), str(uuid.uuid4())
        profile = {"kb_id": kb, "knowledge": [
            {"knowledge_id": kid, "expected_phrase": "RFC 9110"}]}
        profile_path = self.root / "l1-profile.json"
        profile_path.write_text(json.dumps(profile))
        profile_path.chmod(0o600)
        manifest = {"schema":"p2-126-frozen-oracle-v1",
                    "sources":[{"source_id":"public-rfc","sha256":"f"*64}],
                    "l1_profile_sha256":hashlib.sha256(profile_path.read_bytes()).hexdigest()}
        frozen = self.root / "frozen-oracle.json"
        frozen.write_text(json.dumps(manifest))
        frozen.chmod(0o600)
        self.gate["oracle_sha256"] = hashlib.sha256(frozen.read_bytes()).hexdigest()
        self.sign()
        token = self.root / "scoped-key"
        token.write_text("synthetic-readonly-token-no-real-credential")
        token.chmod(0o600)
        def mock(url, secret):
            if "/chunks/" in url:
                return {"success":True,"data":[{"text":"RFC 9110"}]}
            return {"success":True,"data":{"id":kid,"knowledge_base_id":kb}}
        with patch.dict(os.environ, {"P2_SOAK_GATE_KEY_FILE":str(self.key)}):
            ok = business_run(self.root, profile_path, token, transport=mock)
        self.assertEqual(ok["result"],"PASS")
        self.assertEqual(ok["mode"],"soak")
        self.assertEqual(ok["oracle_sha256"],self.gate["oracle_sha256"])
        profile["knowledge"][0]["expected_phrase"] = "tampered"
        profile_path.write_text(json.dumps(profile))
        with patch.dict(os.environ, {"P2_SOAK_GATE_KEY_FILE":str(self.key)}):
            bad = business_run(self.root, profile_path, token, transport=mock)
        self.assertEqual(bad["result"],"FAIL")

if __name__ == "__main__":
    unittest.main()
