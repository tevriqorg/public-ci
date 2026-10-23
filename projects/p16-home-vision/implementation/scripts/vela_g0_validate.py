#!/usr/bin/env python3
"""G0 design-only, fail-closed catalog validator. Not a 15-day acceptance tool."""
import argparse
import copy
import json
import re
import sys
from pathlib import Path

REQUIRED_CATEGORIES = {"core", "fault", "media", "ui", "safety", "resilience", "learning"}
MANDATORY_REQUIRES = {
    "P16-17-1", "P16-17-2", "P16-17-3", "P16-17-4",
    "P16-17-5", "P16-17-6", "P16-9-UI", "P16-11-C",
    "P16-11-D", "P16-11-E", "P16-19-SAFE",
    "P16-19-OPER", "P16-19-LEARN",
}
REQUIRED_FIELDS = {"id", "category", "requirement", "title", "cadence", "owner",
                   "injection", "assertion", "evidence", "severity", "seed",
                   "replay", "on_failure"}
DANGEROUS_COMMANDS = ("rm -rf", "curl http://", "sudo", "hermes", "nvr-production")


def inspect(catalog):
    failures = []
    def require(ok, problem):
        if not ok:
            failures.append(problem)

    require(catalog.get("schema") == "p16.vela-soak.scenario-catalog.v1", "SCHEMA")
    require(catalog.get("design_only") is True, "MUST_BE_DESIGN_ONLY")
    clock = catalog.get("clock") or {}
    require(clock.get("timezone") == "Asia/Singapore", "TIMEZONE")
    require(clock.get("real_days") == 15, "REAL_15_DAYS")
    require(clock.get("no_future_fixture") is True, "FUTURE_FIXTURE")
    require("no retroactive" in clock.get("start_rule", ""), "NO_RETROACTIVE_PASS")
    guards = catalog.get("resource_guards") or {}
    require(guards.get("host_min_free_bytes", 0) >= 6 * 1024**3, "HOST_DISK_GUARD")
    require(0 < guards.get("uat_max_bytes", 0) <= 512 * 1024**2, "UAT_DISK_CAP")
    require(guards.get("delete_raw") is False, "RAW_DELETE_FORBIDDEN")
    require(guards.get("stop_new_work_on_breach") is True, "NO_STOP_ON_GUARD")
    require(set(catalog.get("categories", [])) == REQUIRED_CATEGORIES, "CATEGORIES")
    requirements = catalog.get("requirements") or []
    declared = {r.get("id") for r in requirements if isinstance(r, dict)}
    require(MANDATORY_REQUIRES <= declared, "REQUIREMENTS_MISSING")
    scenario_ids = set()
    covered = set()
    seen_categories = set()
    scenarios = catalog.get("scenarios") or []
    require(len(scenarios) >= 36, "SCENARIOS_TOO_FEW")
    for i, s in enumerate(scenarios):
        if not isinstance(s, dict):
            failures.append(f"SCENARIO_NOT_OBJECT_{i}")
            continue
        missing = REQUIRED_FIELDS - set(s)
        require(not missing, f"SCENARIO_FIELDS_{i}:{sorted(missing)}")
        sid = s.get("id", "")
        require(bool(re.fullmatch(r"S[0-9]{3}", str(sid))), f"INVALID_ID_{i}")
        require(sid not in scenario_ids, f"DUPLICATE_ID_{sid}")
        scenario_ids.add(sid)
        covered.add(s.get("requirement"))
        seen_categories.add(s.get("category"))
        for k in ("title", "injection", "assertion", "evidence", "seed", "owner"):
            require(isinstance(s.get(k), str) and len(s[k]) >= 6, f"EMPTY_{k}_{sid}")
        require(s.get("category") in REQUIRED_CATEGORIES, f"BAD_CATEGORY_{sid}")
        require(s.get("cadence") in ("daily", "rotation"), f"BAD_CADENCE_{sid}")
        require(s.get("severity") in ("P0", "P1", "P2"), f"BAD_SEVERITY_{sid}")
        require(s.get("requirement") in declared, f"UNKNOWN_REQ_{sid}")
        require(s.get("on_failure") == "PRESERVE_REPRO_FIX_REVIEW_TARGETED_FULL_REGRESSION", f"NO_FIX_LOOP_{sid}")
        replay = s.get("replay")
        if not isinstance(replay, dict):
            failures.append(f"REPLAY_NOT_OBJECT_{sid}")
            replay = {}
        require(replay.get("mode") == "isolated-synthetic" and replay.get("scenario_id") == sid,
                f"REPLAY_UNSAFE_{sid}")
        command = replay.get("command")
        require(isinstance(command, str) and command == "p16-soak scenario",
                f"COMMAND_NOT_APPROVED_{sid}")
        require(isinstance(command, str) and not any(x in command.lower() for x in DANGEROUS_COMMANDS),
                f"COMMAND_UNSAFE_{sid}")
    require({f"S{i:03}" for i in range(1, 37)} <= scenario_ids, "FROZEN_SCENARIOS_MISSING")
    daily = {"S001", "S002", "S003", "S004", "S005", "S012", "S016", "S026",
             "S027", "S028", "S029", "S031", "S032", "S034"}
    require(daily == {s.get("id") for s in scenarios if isinstance(s, dict) and s.get("cadence") == "daily"},
            "DAILY_SAFETY_WEAKENED")
    require(MANDATORY_REQUIRES <= covered, "UNCOVERED_REQUIREMENTS")
    require(REQUIRED_CATEGORIES <= seen_categories, "UNCOVERED_CATEGORIES")
    learning = catalog.get("learning") or {}
    require(set(learning.get("classification", [])) ==
            {"product_bug", "harness_bug", "environment", "flaky", "unknown"},
            "MISSING_FAILURE_CLASSES")
    needed = {"observation", "minimal_reproducer", "root_cause", "issue_or_PR",
              "targeted_regression", "full_regression", "vela_recheck",
              "new_permanent_scenario", "three_day_watch"}
    require(needed <= set(learning.get("artifacts", [])), "INCOMPLETE_FIX_LEARNING_LOOP")
    require(learning.get("unknown_is_failure") is True, "UNKNOWN_FAILURE_MUST_FAIL")
    require(learning.get("no_auto_adjust_ground_truth") is True, "GROUND_TRUTH_FREEZE")
    require(learning.get("no_unreviewed_code_merge") is True, "REVIEW_GATE")
    canaries = learning.get("canaries") or []
    require(len(canaries) >= 5, "TOO_FEW_CANARIES")
    for c in canaries:
        require(c.get("detect_by") in scenario_ids, f"CANARY_WITHOUT_ORACLE_{c.get('id')}")
    forbidden = {x.get("area") for x in catalog.get("out_of_scope") or []
                 if x.get("status") == "PROHIBITED"}
    require({"production NVR/OMV cutover", "P2/P20/Hermes/real household media"} <= forbidden,
            "PROHIBITED_SCOPE_MISSING")
    return failures


def selfcheck(catalog):
    """Mutation smoke: validator must reject material design mistakes."""
    changes = [
        ("MISSING_REPLAY_COMMAND", lambda c: c["scenarios"][0]["replay"].pop("command")),
        ("EMPTY_REPLAY_COMMAND", lambda c: c["scenarios"][0]["replay"].update(command="")),
        ("MISSING_CRITICAL_SCENE", lambda c: c.update(scenarios=[s for s in c["scenarios"] if s["id"] != "S007"])),
        ("EXPANDED_DAILY", lambda c: c["scenarios"][5].update(cadence="daily")),
        ("WEAKENED_DAILY", lambda c: c["scenarios"][0].update(cadence="rotation")),
        ("FUTURE_FIXTURE", lambda c: c["clock"].update(no_future_fixture=False)),
        ("NO_FIX_LOOP", lambda c: c["scenarios"][0].update(on_failure="retry_until_green")),
        ("DUPLICATE_ID", lambda c: c["scenarios"][1].update(id=c["scenarios"][0]["id"])),
        ("BROKEN_ORACLE", lambda c: c["scenarios"][0].update(assertion="")),
        ("RAW_DELETE", lambda c: c["resource_guards"].update(delete_raw=True)),
        ("MISSING_CANARY", lambda c: c["learning"].update(canaries=[])),
        ("HIDDEN_UNKNOWN", lambda c: c["learning"].update(unknown_is_failure=False)),
        ("UNCOVERED_REQ", lambda c: c.update(scenarios=[
            s for s in c["scenarios"] if s["requirement"] != "P16-17-1"])),
    ]
    missed = []
    for name, mutate in changes:
        changed = copy.deepcopy(catalog)
        mutate(changed)
        if not inspect(changed):
            missed.append(name)
    return {"injected": len(changes), "detected": len(changes) - len(missed), "missed": missed}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("catalog", type=Path)
    p.add_argument("--selfcheck", action="store_true")
    opts = p.parse_args()
    catalog = json.loads(opts.catalog.read_text(encoding="utf-8"))
    errors = inspect(catalog)
    audit = selfcheck(catalog) if opts.selfcheck else None
    output = {"gate": "G0_design_schema_only", "scenario_count": len(catalog.get("scenarios", [])),
              "requirement_count": len(catalog.get("requirements", [])),
              "errors": errors, "mutation_selfcheck": audit,
              "PASS": not errors and (audit is None or audit["detected"] == audit["injected"])}
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if output["PASS"] else 1


if __name__ == "__main__":
    sys.exit(main())
