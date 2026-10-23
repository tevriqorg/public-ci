"""Clone-only L3 cannot claim PASS on process exit or fabricated replay."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from issue126_canary_l3_worker_restart import validate_replay_proof, active


class L3Proof(unittest.TestCase):
    def test_inactive_preflight_unit_is_not_a_failure_to_check(self):
        with patch("issue126_canary_l3_worker_restart.subprocess.run",
                   return_value=subprocess.CompletedProcess(
                       ["systemctl"], 3, stdout="inactive\n", stderr="")):
            self.assertFalse(active("aibox-uat-soak126-l1-preflight"))

    def test_unknown_unit_is_fail_closed(self):
        with patch("issue126_canary_l3_worker_restart.subprocess.run",
                   return_value=subprocess.CompletedProcess(
                       ["systemctl"], 4, stdout="unknown\n", stderr="")):
            with self.assertRaises(ValueError):
                active("aibox-uat-soak126-l1-preflight")

    def test_exact_real_replay_required(self):
        d = {"result": "PASS_CANARY_AT_REST_ONLY", "replays": 3,
             "revision_count": 1, "raw_bytes_copied": 0,
             "formal_soak_started": False,
             "knowledge_id": "test-knowledge", "resource_ref": "test-resource"}
        self.assertTrue(validate_replay_proof(d))
        for k, v in (
            ("result", "PASS"), ("replays", 2), ("revision_count", 2),
            ("raw_bytes_copied", 1), ("formal_soak_started", True),
            ("knowledge_id", None), ("resource_ref", None),
        ):
            bad = dict(d, **{k: v})
            with self.assertRaises(ValueError, msg=k):
                validate_replay_proof(bad)
        with self.assertRaises(ValueError):
            validate_replay_proof({"result": "PASS_CANARY_AT_REST_ONLY"})


if __name__ == "__main__":
    unittest.main()
