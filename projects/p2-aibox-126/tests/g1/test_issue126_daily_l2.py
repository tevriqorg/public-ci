"""Never mistake a partial daily preflight for Gate0 or 15-day soak."""
import sys
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"scripts"))
from issue126_canary_daily import evaluate_checks, CASES


class DailyL2Gate(unittest.TestCase):
    def example(self):
        return [
            {"name":"at_rest","exit_code":0,"result":"PASS_CANARY_AT_REST_ONLY",
             "replays":3,"revision_count":1,"raw_bytes_copied":0,
             "formal_soak_started":False},
            {"name":"transit","exit_code":0,"result":"PASS_CANARY_TRANSIT_HANDOFF_ONLY",
             "state":"complete","staging_cleaned":True,"formal_soak_started":False},
            {"name":"continuous","exit_code":0,
             "result":"PASS_CANARY_CONTINUOUS_BACKFILL_REPLAY_ONLY",
             "cursor_persisted":True,"real_search_hit":True,"formal_soak_started":False},
            {"name":"scoped_auth","exit_code":0,
             "result":"PRECHECK_PASS_CLONE_KB_SCOPE_ONLY","case_count":10,
             "formal_soak_started":False},
        ]

    def test_complete_subset_is_preflight_only(self):
        self.assertTrue(evaluate_checks(self.example()))
        self.assertEqual(len(CASES),4)

    def test_missing_case_and_fake_generic_pass_are_rejected(self):
        x=self.example()
        self.assertFalse(evaluate_checks(x[:-1]))
        x[2]["result"]="PASS"
        self.assertFalse(evaluate_checks(x))

    def test_false_formal_soak_claim_and_bad_replay_are_rejected(self):
        x=self.example()
        x[0]["formal_soak_started"]=True
        self.assertFalse(evaluate_checks(x))
        x=self.example()
        x[0]["replays"]=1
        self.assertFalse(evaluate_checks(x))

    def test_nonzero_exit_is_never_pass(self):
        x=self.example()
        x[3]["exit_code"]=1
        self.assertFalse(evaluate_checks(x))


if __name__=="__main__":
    unittest.main()
