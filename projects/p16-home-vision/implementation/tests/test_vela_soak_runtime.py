import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "implementation/scripts"))
from vela_soak_runtime import EvidenceStore, continuous, run_cycle
CATALOG = json.loads((ROOT / "design/P16R2_VELA_SOAK_G0_SCENARIOS_v1.json").read_text())


def record(**kw):
    value = dict(run_id="original", source_sha="a"*40, scenario_id="S001", seed="seed-1",
                 status="FAIL", timestamp_utc="2026-10-23T00:00:00+00:00")
    value.update(kw)
    return value


class RuntimeTests(unittest.TestCase):
    def test_original_fail_survives_linked_recovery_and_duplicate_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            store = EvidenceStore(d)
            key = store.append(record())
            before = (store.runs / (key+".json")).read_bytes()
            store.append(record(run_id="recovery", status="PASS", recovery_of="original"))
            with self.assertRaisesRegex(ValueError, "DUPLICATE_RUN_ID"):
                store.append(record(status="PASS"))
            self.assertEqual(before, (store.runs / (key+".json")).read_bytes())
            self.assertEqual(len(store.records()), 2)

    def test_tampered_evidence_stops_further_append(self):
        with tempfile.TemporaryDirectory() as d:
            store = EvidenceStore(d)
            key = store.append(record())
            (store.runs / (key+".json")).write_text(json.dumps(record(status="PASS")))
            with self.assertRaisesRegex(ValueError, "EVIDENCE_HASH_MISMATCH"):
                store.append(record(run_id="next"))

    def test_recovery_requires_same_failed_scenario_and_seed(self):
        with tempfile.TemporaryDirectory() as d:
            store = EvidenceStore(d)
            store.append(record())
            for changes in ({"seed":"different"}, {"scenario_id":"S002"}, {"recovery_of":"absent"}):
                with self.assertRaises(ValueError):
                    store.append(record(**{**dict(run_id="recovery", recovery_of="original"), **changes}))

    def test_missing_handlers_never_credit_cycle_or_day(self):
        with tempfile.TemporaryDirectory() as d:
            result = run_cycle(CATALOG, {}, EvidenceStore(d), source_sha="a"*40, guard=lambda:None)
            self.assertEqual(len(result["results"]),36)
            self.assertFalse(result["complete"])
            self.assertFalse(result["day_credit"])
            self.assertTrue(all(r["status"]=="BLOCKED" for r in result["results"]))

    def test_real_exception_is_retained_as_fail(self):
        def broken(_): raise RuntimeError("do not log secret-bearing exception text")
        with tempfile.TemporaryDirectory() as d:
            store = EvidenceStore(d)
            result=run_cycle(CATALOG,{"S001":broken},store,source_sha="a"*40,guard=lambda:None)
            self.assertEqual(result["results"][0]["status"],"FAIL")
            self.assertNotIn("secret-bearing",json.dumps(store.records()))

    def test_guard_stops_new_work(self):
        def guard(): raise OSError("disk")
        with tempfile.TemporaryDirectory() as d:
            result=run_cycle(CATALOG,{},EvidenceStore(d),source_sha="a"*40,guard=guard)
            self.assertEqual(len(result["results"]),1)
            self.assertFalse(result["complete"])

    def test_continuous_process_runs_until_stop(self):
        calls=[]
        continuous(lambda:calls.append("cycle"),stop_requested=lambda:len(calls)==3,
                   wait=lambda _:None)
        self.assertEqual(calls,["cycle"]*3)

if __name__ == "__main__": unittest.main()
