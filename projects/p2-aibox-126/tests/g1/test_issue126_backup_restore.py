"""Isolated backup/restore safety and independent row-count oracle contracts."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from issue126_canary_backup_restore import safe_database, validate_restore, TABLES


class BackupRestoreContract(unittest.TestCase):
    def test_only_clone_source(self):
        self.assertEqual(safe_database("aibox126_canary_b_20261023"), "aibox126_canary_b_20261023")
        for bad in ("weknora", "postgres", "aiboxacc107", "aibox126_canary_../../postgres",
                    "aibox126_canary_x;DROP DATABASE", "aibox126_canary_\n"):
            with self.assertRaises(ValueError, msg=bad):
                safe_database(bad)

    def test_only_disposable_restore_target(self):
        self.assertEqual(safe_database("aibox126_restoreprobe_123456abcdef", temporary=True),
                         "aibox126_restoreprobe_123456abcdef")
        for bad in ("aibox126_canary_b_20261023", "postgres", "aibox126_restoreprobe_1",
                    "aibox126_restoreprobe_123456abcdef;drop"):
            with self.assertRaises(ValueError, msg=bad):
                safe_database(bad, temporary=True)

    def test_same_count_corruption_must_fail_content_digest(self):
        a = dict(zip(TABLES, (2, 12, 5, 8)))
        good = {name: "a" * 32 for name in TABLES}
        bad = dict(good, knowledges="b" * 32)
        with self.assertRaises(ValueError):
            validate_restore(a, dict(a), dict(a),
                             source_digest=good, restored_digest=bad,
                             before_digest=good)

    def test_restored_real_counts_must_match(self):
        a = dict(zip(TABLES, (2, 12, 5, 8)))
        good = {name: "a" * 32 for name in TABLES}
        self.assertTrue(validate_restore(a, dict(a), dict(a),
                         source_digest=good, restored_digest=good,
                         before_digest=good))
        for table in TABLES:
            b = dict(a); b[table] += 1
            with self.assertRaises(ValueError):
                validate_restore(a, b, a,
                                 source_digest=good, restored_digest=good,
                                 before_digest=good)
            with self.assertRaises(ValueError):
                validate_restore(a, a, b,
                                 source_digest=good, restored_digest=good,
                                 before_digest=good)
        with self.assertRaises(ValueError):
            validate_restore(dict(zip(TABLES, (1, 0, 1, 0))),
                             dict(zip(TABLES, (1, 0, 1, 0))),
                             dict(zip(TABLES, (1, 0, 1, 0))),
                             source_digest=good, restored_digest=good,
                             before_digest=good)


if __name__ == "__main__":
    unittest.main()
