"""#126 fingerprint negative: synthetic fail-closed contract, no private state."""
import pathlib
import json
import subprocess
import tempfile
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
from issue126_fingerprint_negative import mismatched_sha256, rejection_verified


class FingerprintNegativeContract(unittest.TestCase):
    def test_changed_valid_hash_is_not_the_frozen_hash(self):
        source = "a" * 64
        changed = mismatched_sha256(source)
        self.assertEqual(len(changed), 64)
        self.assertNotEqual(changed, source)
        self.assertEqual(mismatched_sha256("0" * 64), "1" + "0" * 63)

    def test_bad_frozen_hash_fails_closed(self):
        for bad in ("z" * 64, "A" * 64, "a" * 63, "", None):
            with self.assertRaises(ValueError):
                mismatched_sha256(bad)

    def test_missing_private_state_is_blocked_not_failed_or_passed(self):
        script = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "issue126_fingerprint_negative.py"
        with tempfile.TemporaryDirectory() as empty:
            r = subprocess.run([sys.executable, str(script), "--state-dir", empty],
                               capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(json.loads(r.stdout)["result"], "PRECHECK_BLOCKED_FINGERPRINT")

    def test_only_explicit_bad_request_rejection_passes(self):
        self.assertTrue(rejection_verified(400, {"success": False}))
        for status, body in ((200, {"success": True}), (200, {"success": False}),
                             (302, {"success": False}), (403, {"success": False}),
                             (500, {"success": False}), (400, {"success": True}),
                             (400, {}), (400, None)):
            self.assertFalse(rejection_verified(status, body))


if __name__ == "__main__":
    unittest.main()
