#!/usr/bin/env python3
"""Pure, deterministic G0/G1 test-plan scheduler. Never executes tests or deploys code."""
from __future__ import annotations
import argparse
import json
import hashlib
import re
from datetime import date, timedelta
from pathlib import Path

SEVERITY = {"P0": 3, "P1": 2, "P2": 1}
SCHEMA = "p16.vela-soak.strategy-plan.v1"
HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{7,64}$")


def _sha(data: object) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _check_incident(bug: object, day: date) -> None:
    if not isinstance(bug, dict) or not isinstance(bug.get("id"), str) or not bug["id"]:
        raise ValueError("INVALID_INCIDENT")
    if bug.get("severity") not in SEVERITY or bug.get("status") not in ("open", "fixed", "observing"):
        raise ValueError("INVALID_INCIDENT")
    if bug.get("classification", "unknown") not in ("product_bug", "harness_bug", "environment", "flaky", "unknown"):
        raise ValueError("INVALID_FAILURE_CLASS")
    try:
        first_seen = date.fromisoformat(bug["first_seen"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("INVALID_FIRST_SEEN") from exc
    if first_seen > day:
        raise ValueError("FUTURE_INCIDENT")
    if not isinstance(bug.get("evidence_hash"), str) or not HASH.fullmatch(bug["evidence_hash"]):
        raise ValueError("INCIDENT_WITHOUT_EVIDENCE")
    ids = bug.get("scenario_ids")
    if not isinstance(ids, list) or not ids or any(not isinstance(x, str) for x in ids):
        raise ValueError("INVALID_SCENARIO_REFERENCES")
    if len(ids) != len(set(ids)):
        raise ValueError("DUPLICATE_SCENARIO_REFERENCES")
    for k in ("recurrence", "blast_radius"):
        value = bug.get(k, 1)
        if type(value) is not int or value < 1 or value > 10:
            raise ValueError("INVALID_RISK_FACTOR")
    if bug.get("status") in ("fixed", "observing"):
        try:
            fixed_date = date.fromisoformat(bug["fixed_date"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("INVALID_FIXED_DATE") from exc
        if fixed_date < first_seen or fixed_date > day:
            raise ValueError("INVALID_FIXED_DATE")



def _incident_priority(bug: dict) -> tuple:
    return (-SEVERITY[bug["severity"]], -min(10, bug.get("recurrence", 1)),
            -min(10, bug.get("blast_radius", 1)), bug["id"])


def plan(catalog: dict, *, first_day: date, day: date, incidents: list[dict],
         watch_until: date | None = None, extension_evidence: str | None = None) -> dict:
    """Produce schedule with non-displaceable mandatory and rotation slots."""
    if day < first_day:
        raise ValueError("OUTSIDE_FROZEN_15D_WINDOW")
    extension = day >= first_day + timedelta(days=15)
    if extension and (watch_until is None or day > watch_until or
                      not isinstance(extension_evidence, str) or not HASH.fullmatch(extension_evidence)):
        raise ValueError("OUTSIDE_FROZEN_15D_WINDOW")
    if catalog.get("schema") != "p16.vela-soak.scenario-catalog.v1":
        raise ValueError("UNAPPROVED_CATALOG")
    from vela_g0_validate import inspect
    if inspect(catalog):
        raise ValueError("UNAPPROVED_CATALOG_INVALID")
    if not isinstance(incidents, list):
        raise ValueError("INVALID_INCIDENT_SET")
    for bug in incidents:
        _check_incident(bug, day)
    bug_ids = [bug["id"] for bug in incidents]
    if len(bug_ids) != len(set(bug_ids)):
        raise ValueError("DUPLICATE_BUG_IDS")
    catalog_ids = {s["id"] for s in catalog["scenarios"]}
    if any(sid not in catalog_ids for bug in incidents for sid in bug["scenario_ids"]):
        raise ValueError("UNKNOWN_SCENARIO_ORACLE")
    scenarios = {s["id"]: s for s in catalog["scenarios"]}
    mandatory = sorted(s["id"] for s in scenarios.values() if s["cadence"] == "daily")
    rotating = sorted(s["id"] for s in scenarios.values() if s["cadence"] == "rotation")
    day_idx = (day-first_day).days
    # Reserved rotation capacity: all non-daily scenarios get a fair pass
    # during the 15 real days, even during bursts of newly discovered defects.
    rotation = [rotating[(day_idx * 3 + k) % len(rotating)]
                for k in range(min(3, len(rotating)))]
    additions = []
    new_case_proposals = []
    blockers = []
    considered = []
    pending = []
    for bug in sorted(incidents, key=_incident_priority):
        if bug.get("classification", "unknown") == "unknown":
            blockers.append({"bug": bug["id"], "reason": "UNCLASSIFIED_FAILURE"})
        if bug["status"] == "open" and bug["severity"] == "P0":
            blockers.append({"bug": bug["id"], "reason": "P0_STOP_AFFECTED_WORK"})
        if bug.get("new_case_proposal"):
            new_case_proposals.append({"bug": bug["id"], "proposal_sha256": _sha(bug["new_case_proposal"]),
                                       "status": "REVIEW_REQUIRED_NOT_EXECUTABLE"})
        if bug.get("status") in ("fixed", "observing"):
            if not isinstance(bug.get("fix_sha"), str) or not COMMIT.fullmatch(bug["fix_sha"]) or not isinstance(bug.get("targeted_pass_evidence"), str) or not HASH.fullmatch(bug["targeted_pass_evidence"]):
                blockers.append({"bug": bug["id"], "reason": "FIX_NOT_VERIFIED"})
                continue
            required = ("full_regression_evidence", "public_ci_evidence", "review_evidence",
                        "permanent_regression_evidence", "adjacent_pass_evidence")
            if any(not isinstance(bug.get(k), str) or not HASH.fullmatch(bug[k]) for k in required):
                blockers.append({"bug": bug["id"], "reason": "FIX_LOOP_INCOMPLETE"})
                continue
            fixed_day = date.fromisoformat(bug["fixed_date"])
            if not fixed_day <= day <= fixed_day + timedelta(days=3):
                continue
        for sid in bug.get("scenario_ids", []):
            if sid not in scenarios:
                blockers.append({"bug": bug["id"], "reason": "UNKNOWN_SCENARIO_ORACLE"})
                continue
            if sid not in mandatory and sid not in rotation and sid not in {x["scenario_id"] for x in considered}:
                considered.append({"bug": bug["id"], "scenario_id": sid})
                if len(additions) < 3:
                    additions.append(sid)
                else:
                    pending.append({"bug": bug["id"], "scenario_id": sid})
    if pending:
        blockers.append({"bug": "ADAPTIVE_BUDGET", "reason": "REQUIRED_TARGETED_TESTS_PENDING"})
    selected = mandatory + [s for s in rotation if s not in mandatory] + additions
    if len(selected) != len(set(selected)):
        raise AssertionError("DUPLICATE_TEST_SELECTION")
    return {"schema": SCHEMA, "catalog_sha256": _sha(catalog),
            "incident_set_sha256": _sha(incidents), "day_sgt": day.isoformat(),
            "window_start_sgt": first_day.isoformat(), "date_offset": day_idx,
            "phase": "clean_watch_extension" if extension else "baseline_15d",
            "baseline_end_sgt": (first_day + timedelta(days=14)).isoformat(),
            "extension_until_sgt": watch_until.isoformat() if extension else None,
            "extension_evidence": extension_evidence if extension else None,
            "mandatory": mandatory, "rotation": rotation, "adaptive": additions,
            "adaptive_pending": pending,
            "scenario_ids": selected, "blockers": blockers,
            "new_case_proposals": new_case_proposals,
            "evidence_requirement": "record all failures and original SHA; never overwrite PASS",
            "gate_to_run": "HOLD_AFFECTED_WORK" if blockers else "TEST_PLAN_ONLY",
            "mutations_executed": False}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--catalog", required=True, type=Path)
    p.add_argument("--start", required=True, type=date.fromisoformat)
    p.add_argument("--day", required=True, type=date.fromisoformat)
    p.add_argument("--incidents", type=Path)
    p.add_argument("--watch-until", type=date.fromisoformat)
    p.add_argument("--extension-evidence")
    options = p.parse_args()
    catalog = json.loads(options.catalog.read_text(encoding="utf-8"))
    bugs = json.loads(options.incidents.read_text(encoding="utf-8")) if options.incidents else []
    print(json.dumps(plan(catalog, first_day=options.start, day=options.day, incidents=bugs,
                     watch_until=options.watch_until, extension_evidence=options.extension_evidence),
                     indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
