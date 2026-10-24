import tempfile
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"deploy/aibox-0.8/vela-uat/soak"))
from risk_scheduler import propose
from soak_engine import failing_incident,read_events


class AdaptiveTest(unittest.TestCase):
    def test_critical_bug_prioritized_but_never_auto_executes(self):
        with tempfile.TemporaryDirectory() as temp:
            state=Path(temp)
            before=propose(state,target="clone",limit=8,timestamp="2026-10-22T22:00:00Z")
            failing_incident(state,case_id="TR03",signature="race_condition",source_id="temp-p2-asset")
            after=propose(state,target="clone",limit=8,timestamp="2026-10-22T22:00:00Z")
            self.assertIn("TR03",{x["case_id"] for x in after["cases"]})
            self.assertTrue(any(x["case_id"]=="TR03" for x in after["cases"][:3]))
            self.assertNotEqual(before["cases"],after["cases"])
            self.assertFalse(after["execution_authorized"])
            self.assertFalse(after["changes_frozen_baseline"])
            self.assertTrue(all(x["layer"]=="clone" for x in after["cases"]))
            self.assertEqual(len(read_events(state/"case_proposals.jsonl")),1)

    def test_only_readonly_or_live_not_faults_in_live(self):
        with tempfile.TemporaryDirectory() as temp:
            state=Path(temp)
            failing_incident(state,case_id="SC01",signature="resource_permission",source_id="seed-test")
            live=propose(state,target="live",limit=12)
            self.assertTrue(all(x["layer"] in ("live","host_read_only") for x in live["cases"]))
            self.assertNotIn("SC01",{x["case_id"] for x in live["cases"]})
            self.assertRaises(ValueError,propose,state,target="production",limit=8)


if __name__=="__main__":
    unittest.main()
