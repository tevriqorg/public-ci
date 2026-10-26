import copy
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "implementation/scripts"))
import vela_soak_runtime
from vela_soak_runtime import EvidenceStore, continuous, run_cycle, run_scenario, cycle_lock, command_handler, record_daily_cycle
CATALOG = json.loads((ROOT / "design/P16R2_VELA_SOAK_G0_SCENARIOS_v1.json").read_text())


def record(**kw):
    value = dict(run_id="original", source_sha="a"*40, scenario_id="S001", seed="seed-1",
                 status="FAIL", timestamp_utc="2026-10-23T00:00:00+00:00")
    value.update(kw)
    return value


class RuntimeTests(unittest.TestCase):
    def test_s028_outage_catchup_keeps_missed_days_and_rejects_second_writer(self):
        with tempfile.TemporaryDirectory() as fresh:
            late_first = record_daily_cycle(
                fresh, scheduled_start="2026-10-22", observed_day="2026-10-23",
                run_id="first-late-catchup", complete=True,
                observed_at="2026-10-25T00:20:00+08:00")
            self.assertEqual(late_first["schedule"], [
                ("2026-10-22", "MISSING"), ("2026-10-23", "MISSING"),
                ("2026-10-24", "MISSING")])
            self.assertEqual(late_first["attempt"],
                             ("first-late-catchup", "2026-10-23", "PASS"))
        with tempfile.TemporaryDirectory() as d:
            first = record_daily_cycle(d, scheduled_start="2026-10-22",
                                       observed_day="2026-10-25", run_id="after-reboot",
                                       complete=True,
                                       observed_at="2026-10-25T00:15:00+08:00")
            self.assertEqual(first["schedule"], [
                ("2026-10-22", "MISSING"), ("2026-10-23", "MISSING"),
                ("2026-10-24", "MISSING"), ("2026-10-25", "PASS")])
            recovery = record_daily_cycle(d, scheduled_start="2026-10-22",
                                          observed_day="2026-10-23", run_id="catchup-23",
                                          complete=True,
                                          observed_at="2026-10-25T00:20:00+08:00")
            self.assertEqual(recovery["schedule"], first["schedule"])
            with sqlite3.connect(Path(d) / "daily-ledger.sqlite3") as db:
                attempts = db.execute(
                    "SELECT day, outcome FROM daily_attempts ORDER BY run_id").fetchall()
            self.assertEqual(attempts, [("2026-10-25", "PASS"), ("2026-10-23", "PASS")])
            with cycle_lock(d):
                with self.assertRaises(BlockingIOError):
                    with cycle_lock(d):
                        pass

    def test_s028_older_catchup_leaves_current_day_open_for_daily_run(self):
        with tempfile.TemporaryDirectory() as d:
            catchup = record_daily_cycle(
                d, scheduled_start="2026-10-22", observed_day="2026-10-23",
                run_id="catchup-23", complete=True,
                observed_at="2026-10-25T00:20:00+08:00")
            self.assertEqual(catchup["schedule"], [
                ("2026-10-22", "MISSING"), ("2026-10-23", "MISSING"),
                ("2026-10-24", "MISSING")])
            daily = record_daily_cycle(
                d, scheduled_start="2026-10-22", observed_day="2026-10-25",
                run_id="daily-25", complete=True,
                observed_at="2026-10-25T00:25:00+08:00")
            self.assertEqual(daily["schedule"], catchup["schedule"] + [
                ("2026-10-25", "PASS")])

    def test_s030_bug_reproducer_recovery_and_adjacent_regression_are_immutable(self):
        today = datetime.now(ZoneInfo("Asia/Singapore")).date()
        start = (today - timedelta(days=3)).isoformat()
        old_day = (today - timedelta(days=2)).isoformat()
        today_text = today.isoformat()

        def exercise(root, recorder):
            recorder(root, scheduled_start=start, observed_day=old_day,
                     run_id="late-catchup", complete=True,
                     observed_at=f"{today_text}T00:20:00+08:00")
            return recorder(root, scheduled_start=start, observed_day=today_text,
                            run_id="on-date", complete=True,
                            observed_at=f"{today_text}T00:25:00+08:00")

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store = EvidenceStore(root / "learning-evidence")
            original = record(run_id="s030-original-failure", scenario_id="S030",
                              seed="p16-g0-v1-S030",
                              source_sha="2b9b23ec882bb57477f5214abc0444fb8f8bb9ca",
                              result={"bug": "late catch-up finalized current day",
                                      "review_comment": "4373031367"})
            failure_hash = store.append(original)
            failure_bytes = (store.runs / f"{failure_hash}.json").read_bytes()

            real_recorder = vela_soak_runtime.record_daily_cycle

            def retroactive_missing_mutant(*args, **kwargs):
                result = real_recorder(*args, **kwargs)
                if kwargs["run_id"] == "late-catchup":
                    with sqlite3.connect(Path(args[0]) / "daily-ledger.sqlite3") as db:
                        db.execute("INSERT OR IGNORE INTO daily_schedule VALUES (?, 'MISSING')",
                                   (today_text,))
                    result["schedule"].append((today_text, "MISSING"))
                else:
                    with sqlite3.connect(Path(args[0]) / "daily-ledger.sqlite3") as db:
                        result["schedule"] = db.execute(
                            "SELECT day, state FROM daily_schedule ORDER BY day").fetchall()
                return result

            with tempfile.TemporaryDirectory() as mutant_root:
                with patch.object(vela_soak_runtime, "record_daily_cycle",
                                  side_effect=retroactive_missing_mutant):
                    with self.assertRaises(AssertionError):
                        self.assertEqual(exercise(mutant_root, vela_soak_runtime.record_daily_cycle)
                                         ["schedule"][-1], (today_text, "PASS"))

            with tempfile.TemporaryDirectory() as fixed_root:
                fixed = exercise(fixed_root, real_recorder)
                self.assertEqual(fixed["schedule"][-1], (today_text, "PASS"))
                self.assertEqual(fixed["schedule"][-2][1], "MISSING")

            with tempfile.TemporaryDirectory() as adjacent_root:
                adjacent = real_recorder(
                    adjacent_root, scheduled_start=today_text, observed_day=today_text,
                    run_id="on-date-failure", complete=False,
                    observed_at=f"{today_text}T00:30:00+08:00")
                self.assertEqual(adjacent["schedule"], [(today_text, "FAIL")])

            recovery = record(run_id="s030-fixed-recovery", scenario_id="S030",
                              seed="p16-g0-v1-S030", status="PASS",
                              source_sha="6dbf46207f4ebe9f635cabd45f44fbc07485570b",
                              recovery_of=original["run_id"],
                              result={"targeted": "PASS", "adjacent": "PASS",
                                      "fix_source": "6dbf46207f4ebe9f635cabd45f44fbc07485570b"})
            store.append(recovery)
            self.assertEqual((store.runs / f"{failure_hash}.json").read_bytes(), failure_bytes)
            records = {item["run_id"]: item for item in store.records()}
            self.assertEqual(records[original["run_id"]]["status"], "FAIL")
            self.assertEqual(records[recovery["run_id"]]["status"], "PASS")
            self.assertEqual(records[recovery["run_id"]]["recovery_of"], original["run_id"])

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

    def test_exact_scenario_replay_validates_full_catalog_and_persists_one_result(self):
        with tempfile.TemporaryDirectory() as d:
            store = EvidenceStore(d)
            result = run_scenario(CATALOG, "S020", {"S020": lambda _: {"status": "PASS"}},
                                  store, source_sha="a"*40, guard=lambda: None)
            self.assertEqual([row["scenario_id"] for row in result["results"]], ["S020"])
            self.assertTrue(result["complete"])
            self.assertFalse(result["day_credit"])
            self.assertEqual([row["scenario_id"] for row in store.records()], ["S020"])

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
