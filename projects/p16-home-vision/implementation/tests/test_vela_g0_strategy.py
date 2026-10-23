"""Design-time tests: prove G0 test planning changes on incidents without losing safety coverage."""
import json
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "implementation" / "scripts"))
from vela_soak_strategy import plan

CATALOG = json.loads((ROOT / "design" / "P16R2_VELA_SOAK_G0_SCENARIOS_v1.json").read_text())
T0 = date(2026, 10, 24)

def incident(**kw):
    d = {"id": "BUG-1", "severity": "P1", "status": "open",
         "classification": "product_bug", "first_seen": "2026-10-24",
         "evidence_hash": "sha256:" + "a" * 64, "scenario_ids": ["S019"],
         "recurrence": 2, "blast_radius": 2}
    if kw.get("status") in ("fixed", "observing"):
        for field in ("full_regression_evidence", "public_ci_evidence", "review_evidence",
                      "permanent_regression_evidence", "adjacent_pass_evidence"):
            d[field] = "sha256:" + "b" * 64
    d.update(kw)
    return d

class G0StrategyTests(unittest.TestCase):
    def test_baseline_mandatory_never_displaced(self):
        before = plan(CATALOG, first_day=T0, day=T0, incidents=[])
        after = plan(CATALOG, first_day=T0, day=T0, incidents=[incident()])
        self.assertEqual(before["mandatory"], after["mandatory"])
        self.assertEqual(before["rotation"], after["rotation"])
        self.assertEqual(len(after["rotation"]), 3)
        self.assertIn("S019", after["scenario_ids"])
        self.assertNotIn("S019", before["scenario_ids"])
        self.assertLessEqual(len(after["adaptive"]), 3)

    def test_full_15day_rotation_coverage_including_when_many_bugs(self):
        rotation = {s["id"] for s in CATALOG["scenarios"] if s["cadence"] == "rotation"}
        covered = set()
        for i in range(15):
            p = plan(CATALOG, first_day=T0, day=T0+timedelta(days=i),
                     incidents=[incident(id=f"BUG-{n}",scenario_ids=["S008"]) for n in range(10)])
            covered.update(p["rotation"])
        self.assertEqual(covered, rotation)

    def test_p0_stop_never_swallowed(self):
        p = plan(CATALOG, first_day=T0, day=T0,
                 incidents=[incident(severity="P0", classification="unknown")])
        self.assertEqual(p["gate_to_run"], "HOLD_AFFECTED_WORK")
        self.assertIn("P0_STOP_AFFECTED_WORK", [b["reason"] for b in p["blockers"]])
        self.assertIn("UNCLASSIFIED_FAILURE", [b["reason"] for b in p["blockers"]])

    def test_fixed_bug_gets_three_days_of_targeted_tests(self):
        bug = incident(status="fixed", fixed_date="2026-10-24",
                       fix_sha="deadbeef", targeted_pass_evidence="sha256:" + "f" * 64)
        for i in range(3):
            p = plan(CATALOG, first_day=T0, day=T0+timedelta(days=i),incidents=[bug])
            self.assertIn("S019", p["scenario_ids"])
        p = plan(CATALOG, first_day=T0, day=T0+timedelta(days=3),incidents=[bug])
        self.assertNotIn("S019", p["adaptive"])

    def test_fake_fix_is_not_accepted(self):
        p=plan(CATALOG, first_day=T0, day=T0,
               incidents=[incident(status="fixed",fixed_date="2026-10-24")])
        self.assertEqual(p["gate_to_run"], "HOLD_AFFECTED_WORK")
        self.assertIn("FIX_NOT_VERIFIED", [b["reason"] for b in p["blockers"]])

    def test_unapproved_learning_proposal_not_auto_executed(self):
        p=plan(CATALOG, first_day=T0, day=T0,incidents=[
            incident(new_case_proposal="untrusted code from a bug report")])
        self.assertEqual(p["new_case_proposals"][0]["status"],
                         "REVIEW_REQUIRED_NOT_EXECUTABLE")
        self.assertNotIn("untrusted code from a bug report", p["scenario_ids"])

    def test_bad_input_and_retroactive_days_rejected(self):
        with self.assertRaisesRegex(ValueError,"OUTSIDE"):
            plan(CATALOG, first_day=T0, day=T0-timedelta(days=1),incidents=[])
        with self.assertRaisesRegex(ValueError,"OUTSIDE"):
            plan(CATALOG, first_day=T0, day=T0+timedelta(days=15),incidents=[])
        with self.assertRaisesRegex(ValueError,"INCIDENT_WITHOUT_EVIDENCE"):
            plan(CATALOG, first_day=T0, day=T0,incidents=[incident(evidence_hash="")])

    def test_risk_budget_keeps_pending_and_respects_severity(self):
        bugs = [
            incident(id="BUG-P2", severity="P2", scenario_ids=["S019"]),
            incident(id="BUG-P1-A", severity="P1", scenario_ids=["S020"]),
            incident(id="BUG-P1-B", severity="P1", scenario_ids=["S021"]),
            incident(id="BUG-P0", severity="P0", scenario_ids=["S022"]),
        ]
        p = plan(CATALOG, first_day=T0, day=T0, incidents=bugs)
        self.assertEqual(p["adaptive"], ["S022", "S020", "S021"])
        self.assertEqual(p["adaptive_pending"], [{"bug": "BUG-P2", "scenario_id": "S019"}])
        self.assertEqual(p["gate_to_run"], "HOLD_AFFECTED_WORK")
        self.assertTrue(any(b["reason"] == "P0_STOP_AFFECTED_WORK" for b in p["blockers"]))

    def test_catalog_retains_repeatable_seed_and_unique_requirements(self):
        ids = [x["id"] for x in CATALOG["scenarios"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(x["seed"] == "p16-g0-v1-" + x["id"]
                            for x in CATALOG["scenarios"]))
        self.assertTrue(all(x["requirement"] in {r["id"] for r in CATALOG["requirements"]}
                            for x in CATALOG["scenarios"]))

    def test_unclassified_without_field_blocks_execution(self):
        bug = incident()
        del bug["classification"]
        p = plan(CATALOG, first_day=T0, day=T0, incidents=[bug])
        self.assertEqual(p["gate_to_run"], "HOLD_AFFECTED_WORK")
        self.assertIn("UNCLASSIFIED_FAILURE", [x["reason"] for x in p["blockers"]])

    def test_malformed_incidents_fail_closed_even_before_priority_sort(self):
        cases = [
            incident(severity="P4"), incident(status="rejected"),
            incident(first_seen="not-a-date"), incident(first_seen="2026-10-27"),
            incident(evidence_hash="sha256:not-a-hash"),
            incident(scenario_ids=[]), incident(scenario_ids=["S019", "S019"]),
            incident(recurrence=0), incident(blast_radius="high"),
            incident(status="fixed", fix_sha="deadbeef", targeted_pass_evidence="sha256:" + "f"*64),
            incident(status="fixed", fixed_date="2030-01-01", fix_sha="deadbeef", targeted_pass_evidence="sha256:" + "f"*64),
        ]
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                plan(CATALOG, first_day=T0, day=T0, incidents=[bad])
        with self.assertRaisesRegex(ValueError, "DUPLICATE_BUG_IDS"):
            plan(CATALOG, first_day=T0, day=T0, incidents=[incident(), incident()])
        with self.assertRaisesRegex(ValueError, "INVALID_INCIDENT"):
            plan(CATALOG, first_day=T0, day=T0, incidents=[{}])

    def test_unknown_scenario_never_escapes_after_three_days(self):
        bug=incident(status="fixed", fixed_date="2026-10-24", fix_sha="deadbeef",
                     targeted_pass_evidence="sha256:" + "f"*64, scenario_ids=["S999"])
        with self.assertRaisesRegex(ValueError, "UNKNOWN_SCENARIO_ORACLE"):
            plan(CATALOG, first_day=T0, day=T0+timedelta(days=4), incidents=[bug])

    def test_catalog_tampering_and_output_serialization(self):
        import copy
        bad = copy.deepcopy(CATALOG)
        bad["resource_guards"]["delete_raw"] = True
        with self.assertRaisesRegex(ValueError, "UNAPPROVED_CATALOG_INVALID"):
            plan(bad, first_day=T0, day=T0, incidents=[])
        p=plan(CATALOG,first_day=T0,day=T0,incidents=[incident()])
        json.dumps(p, allow_nan=False)
        self.assertEqual(len(p["catalog_sha256"]), 64)
        self.assertEqual(len(p["incident_set_sha256"]), 64)
        self.assertEqual(p["new_case_proposals"], [])

    def test_learning_suggestion_redacted_and_observing_period(self):
        bug = incident(status="observing",fixed_date="2026-10-24",fix_sha="deadbeef",
                       targeted_pass_evidence="sha256:"+"e"*64,
                       new_case_proposal="SENSITIVE_UNREVIEWED_TEST_SOURCE")
        p=plan(CATALOG,first_day=T0,day=T0,incidents=[bug])
        self.assertIn("S019",p["adaptive"])
        self.assertIn("proposal_sha256",p["new_case_proposals"][0])
        self.assertNotIn("SENSITIVE_UNREVIEWED_TEST_SOURCE",json.dumps(p))
        p3=plan(CATALOG,first_day=T0,day=T0+timedelta(days=3),incidents=[bug])
        self.assertNotIn("S019",p3["adaptive"])

    def test_fixed_bug_cannot_age_out_with_incomplete_loop(self):
        for field in ("full_regression_evidence", "public_ci_evidence", "review_evidence",
                      "permanent_regression_evidence", "adjacent_pass_evidence"):
            bug = incident(status="fixed", fixed_date="2026-10-24", fix_sha="deadbeef",
                           targeted_pass_evidence="sha256:" + "f" * 64)
            del bug[field]
            with self.subTest(field=field):
                p = plan(CATALOG, first_day=T0, day=T0+timedelta(days=4), incidents=[bug])
                self.assertEqual(p["gate_to_run"], "HOLD_AFFECTED_WORK")
                self.assertIn("FIX_LOOP_INCOMPLETE", [x["reason"] for x in p["blockers"]])

    def test_duplicate_pending_does_not_hide_unexecuted_risk(self):
        bugs = [incident(id=f"BUG-{i}", scenario_ids=[sid]) for i,sid in enumerate(
            ["S019", "S020", "S021", "S022", "S022", "S023"])]
        p = plan(CATALOG, first_day=T0, day=T0, incidents=bugs)
        self.assertEqual([x["scenario_id"] for x in p["adaptive_pending"]], ["S022", "S023"])
        self.assertEqual(p["gate_to_run"], "HOLD_AFFECTED_WORK")

    def test_idempotent_plan_no_file_writes(self):
        p1 = plan(CATALOG,first_day=T0,day=T0,incidents=[incident()])
        p2 = plan(CATALOG,first_day=T0,day=T0,incidents=[incident()])
        self.assertEqual(p1,p2)
        self.assertFalse(p1["mutations_executed"])

if __name__ == "__main__":
    unittest.main()
