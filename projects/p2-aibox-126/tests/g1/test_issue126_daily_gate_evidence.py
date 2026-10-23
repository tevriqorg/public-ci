"""Public, synthetic regression for daily coverage gate; no Vela private state."""
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


class DailyEvidenceGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.key = self.root / "approval.key"
        self.key.write_bytes(os.urandom(40))
        self.key.chmod(0o600)
        self.start = dt.datetime(2026, 10, 20, tzinfo=dt.timezone.utc)
        self.gate = {
            "schema": "p2-soak-gate-v1", "trial_id": "P2-126-public-synthetic",
            "start_utc": self.start.isoformat(), "matrix_sha256": FROZEN_MATRIX_SHA256,
            "oracle_sha256": "a" * 64, "p2_sha": "b" * 40,
            "weknora_sha": "c" * 40, "runtime_digest": "sha256:" + "d" * 64,
            "rollback_snapshot": "synthetic", "approval_evidence_sha256": "e" * 64,
            "at_rest_receipt_sha256": "1" * 64,
            "transit_receipt_sha256": "2" * 64,
            "continuous_receipt_sha256": "3" * 64,
        }
        signed = dict(self.gate)
        signed["signature_hmac_sha256"] = hmac.new(
            self.key.read_bytes(), canonical_payload(signed), hashlib.sha256
        ).hexdigest()
        path = self.root / "gate.json"
        path.write_text(json.dumps(signed))
        path.chmod(0o600)

    def daily(self, when, mode):
        append_event(self.root / "daily.jsonl", {
            "timestamp_utc": when.isoformat(), "mode": mode,
            "trial_id": self.gate["trial_id"], "result": "PASS",
            "oracle_sha256": self.gate["oracle_sha256"],
            "formats": ["text", "pdf"], "replay_pass": True,
            "incremental_pass": True,
        })

    def test_future_records_do_not_preload_fifteen_days(self):
        for day in range(15):
            self.daily(self.start + dt.timedelta(days=day, minutes=10), "soak")
        result = evaluate(self.root, now=self.start + dt.timedelta(hours=25),
                          gate_key=self.key)
        self.assertEqual(result["daily_days"], 2)
        self.assertEqual(result["verdict"], "BLOCKED")

    def test_preflight_claim_does_not_count_as_daily_soak(self):
        self.daily(self.start + dt.timedelta(hours=1), "preflight")
        result = evaluate(self.root, now=self.start + dt.timedelta(hours=26),
                          gate_key=self.key)
        self.assertEqual(result["daily_days"], 0)


if __name__ == "__main__":
    unittest.main()
