"""Issue #126: a bare L1 PASS row must not count without hashed probe evidence."""
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy/aibox-0.8/vela-uat/soak"))
from gate_guard import FROZEN_MATRIX_SHA256, canonical_payload
from soak_engine import append_event, evaluate


class L1ArtifactIntegrity(unittest.TestCase):
    def test_forged_l1_pass_row_without_probe_artifact_cannot_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / "gate.key"
            key.write_bytes(os.urandom(40))
            key.chmod(0o600)
            start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
            gate = {
                "schema": "p2-soak-gate-v1", "trial_id": "P2-126-l1-artifact-red",
                "start_utc": start.isoformat(), "matrix_sha256": FROZEN_MATRIX_SHA256,
                "oracle_sha256": "a"*64, "p2_sha": "b"*40,
                "weknora_sha": "c"*40, "runtime_digest": "sha256:"+"d"*64,
                "rollback_snapshot": "synthetic",
                "approval_evidence_sha256": "e"*64,
                "at_rest_receipt_sha256": "1"*64,
                "transit_receipt_sha256": "2"*64,
                "continuous_receipt_sha256": "3"*64,
            }
            gate["signature_hmac_sha256"] = hmac.new(
                key.read_bytes(), canonical_payload(gate), hashlib.sha256).hexdigest()
            gate_file = root / "gate.json"
            gate_file.write_text(json.dumps(gate))
            gate_file.chmod(0o600)
            append_event(root / "business.jsonl", {
                "case_id": "KB01", "mode": "soak", "result": "PASS",
                "trial_id": gate["trial_id"], "oracle_sha256": gate["oracle_sha256"],
                "timestamp_utc": (start + dt.timedelta(minutes=20)).isoformat(),
                "knowledge_count": 1,
                "evidence_sha256": "f"*64,
            })
            out = evaluate(root, now=start+dt.timedelta(hours=1), gate_key=key)
            self.assertEqual(out["l1_slots"], 0,
                "bare PASS row with arbitrary evidence SHA must not count")

    def test_signed_gate_and_mock_api_cannot_bypass_runtime_process_identity(self):
        from unittest.mock import patch
        import uuid
        from business_probe import run as business_run
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / "gate.key"
            key.write_bytes(os.urandom(40)); key.chmod(0o600)
            kb, kid = str(uuid.uuid4()), str(uuid.uuid4())
            profile = root / "profile.json"
            profile.write_text(json.dumps({"kb_id":kb,"knowledge":[
                {"knowledge_id":kid,"expected_phrase":"RFC 9110"}]}))
            profile.chmod(0o600)
            manifest = {"schema":"p2-126-frozen-oracle-v1",
                        "sources":[{"source_id":"public-rfc","sha256":"f"*64}],
                        "l1_profile_sha256":hashlib.sha256(profile.read_bytes()).hexdigest()}
            oracle = root / "frozen-oracle.json"
            oracle.write_text(json.dumps(manifest)); oracle.chmod(0o600)
            gate = {"schema":"p2-soak-gate-v1","trial_id":"P2-126-runtime-red",
                    "start_utc":(dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=1)).isoformat(),
                    "matrix_sha256":FROZEN_MATRIX_SHA256,
                    "oracle_sha256":hashlib.sha256(oracle.read_bytes()).hexdigest(),
                    "p2_sha":"b"*40,"weknora_sha":"c"*40,
                    "runtime_digest":"sha256:"+"d"*64,"rollback_snapshot":"synthetic",
                    "approval_evidence_sha256":"e"*64,
                    "at_rest_receipt_sha256":"1"*64,
                    "transit_receipt_sha256":"2"*64,
                    "continuous_receipt_sha256":"3"*64}
            gate["signature_hmac_sha256"]=hmac.new(
                key.read_bytes(),canonical_payload(gate),hashlib.sha256).hexdigest()
            g=root/"gate.json";g.write_text(json.dumps(gate));g.chmod(0o600)
            token=root/"read.key";token.write_text("synthetic-scoped-key-not-real");token.chmod(0o600)
            def mock(url,secret):
                if "/chunks/" in url:
                    return {"success":True,"data":[{"content":"RFC 9110"}]}
                return {"success":True,"data":{"id":kid,"knowledge_base_id":kb}}
            with patch.dict(os.environ,{"P2_SOAK_GATE_KEY_FILE":str(key)}):
                out=business_run(root,profile,token,transport=mock)
            self.assertNotEqual(out["result"],"PASS",
                "signed Gate alone cannot authorize L1 without actual canary process guard")



if __name__ == "__main__":
    unittest.main()
