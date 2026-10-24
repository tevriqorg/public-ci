import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "implementation/scripts"))
from vela_soak_runtime import EvidenceStore, continuous, run_cycle, cycle_lock, command_handler
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
                store.records()
            with self.assertRaisesRegex(ValueError, "EVIDENCE_HASH_MISMATCH"):
                EvidenceStore(d)

    def test_append_does_not_rescan_history_and_restart_rebuilds_index(self):
        with tempfile.TemporaryDirectory() as d:
            store = EvidenceStore(d)
            store.append(record())
            store.records = lambda: self.fail("append must use indexed lookup")
            for i in range(100):
                store.append(record(run_id=f"run-{i}"))
            restarted = EvidenceStore(d)
            with self.assertRaisesRegex(ValueError, "DUPLICATE_RUN_ID"):
                restarted.append(record())
            restarted.append(record(run_id="recovered", status="PASS", recovery_of="original"))
            self.assertEqual(len(restarted.records()), 102)

    def test_two_store_instances_share_index(self):
        with tempfile.TemporaryDirectory() as d:
            first, second = EvidenceStore(d), EvidenceStore(d)
            first.append(record())
            with self.assertRaisesRegex(ValueError, "DUPLICATE_RUN_ID"):
                second.append(record())
            second.append(record(run_id="recovered", status="PASS", recovery_of="original"))
            self.assertEqual(len(first.records()), 2)

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

    def test_cycle_lock_rejects_second_writer_and_releases(self):
        with tempfile.TemporaryDirectory() as d:
            with cycle_lock(d):
                with self.assertRaises(BlockingIOError):
                    with cycle_lock(d):
                        self.fail("second writer entered")
            store = EvidenceStore(d)
            with cycle_lock(d):
                with self.assertRaises(BlockingIOError):
                    run_cycle(CATALOG, {}, store, source_sha="a"*40, guard=lambda:None)

    def test_surviving_store_recovers_interrupted_publication(self):
        with tempfile.TemporaryDirectory() as d:
            first, survivor = EvidenceStore(d), EvidenceStore(d)
            import sqlite3
            with sqlite3.connect(first.index_path) as db:
                first._append_locked(record(), db)
            (Path(d) / ".publication-pending").touch()
            with self.assertRaisesRegex(ValueError, "DUPLICATE_RUN_ID"):
                survivor.append(record(status="PASS"))
            survivor.append(record(run_id="recovered", status="PASS", recovery_of="original"))
            self.assertEqual(len(EvidenceStore(d).records()), 2)

    def test_competing_constructor_cannot_interrupt_cycle(self):
        with tempfile.TemporaryDirectory() as d:
            store = EvidenceStore(d)
            with cycle_lock(d):
                with self.assertRaises(BlockingIOError):
                    EvidenceStore(d)
                store.append(record())
            self.assertEqual(len(store.records()), 1)

    def test_real_subprocess_timeout_is_failure_and_loop_can_continue(self):
        slow = command_handler([sys.executable, "-c", "import time; time.sleep(60)"],
                               cwd=ROOT, timeout_seconds=0.05)
        outcome = slow({})
        self.assertEqual(outcome["status"], "FAIL")
        self.assertTrue(outcome["timed_out"])
        good = command_handler([sys.executable, "-c", "pass"], cwd=ROOT, timeout_seconds=5)
        self.assertEqual(good({})["status"], "PASS")
        bad = command_handler([sys.executable, "-c", "raise SystemExit(7)"],
                              cwd=ROOT, timeout_seconds=5)
        self.assertEqual(bad({})["exit_code"], 7)

    def test_continuous_process_runs_until_stop(self):
        calls=[]
        continuous(lambda:calls.append("cycle"),stop_requested=lambda:len(calls)==3,
                   wait=lambda _:None)
        self.assertEqual(calls,["cycle"]*3)

if __name__ == "__main__": unittest.main()
