"""Public code-only cross-KB regression: frozen matrix + isolated learning policy."""
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOAK = ROOT / "deploy/aibox-0.8/vela-uat/soak"
FROZEN_MATRIX_SHA256 = "c3b3d89b96f1008e171831780b447bbc8d56e1f218d4651236ad7b9fb962b2e1"

class CrossKBLearning(unittest.TestCase):
    def test_frozen_matrix_and_cross_kb_clone_isolation(self):
        matrix = SOAK / "cases.json"
        self.assertEqual(hashlib.sha256(matrix.read_bytes()).hexdigest(), FROZEN_MATRIX_SHA256)
        cases = {c["id"]: c for c in json.loads(matrix.read_text())["cases"]}
        for case in ("SC01", "SC02"):
            self.assertIn(case, cases)
            self.assertEqual(cases[case]["layer"], "clone")
        rules = json.loads((SOAK / "learning_rules.json").read_text())
        policy = rules["policy"]
        self.assertFalse(policy["auto_modify_original_oracle"])
        self.assertFalse(policy["auto_weaken_assertions"])
        self.assertFalse(policy["destructive_in_live_uat_allowed"])
        self.assertTrue(policy["require_red_before_green"])
        target = {r["signature"]: r for r in rules["rules"]}["resource_permission"]
        self.assertTrue(target["only_in_isolated_clone_for_faults"])
        self.assertTrue({"SC01", "SC02"} <= set(target["prioritized_case_ids"]))
        self.assertTrue({"same_tenant_foreign_kb", "cross_kb_external_source_ref"} <= set(target["expansion_axes"]))

if __name__ == "__main__":
    unittest.main()
