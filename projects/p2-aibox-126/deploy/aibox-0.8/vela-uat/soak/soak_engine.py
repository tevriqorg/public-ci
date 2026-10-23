#!/usr/bin/env python3
"""P2 #126 fail-closed evidence, incidents, learning and 360-slot evaluation.

No test runner or repair agent has implicit access to production assets.
Never auto-promote a long trial from a timestamp, a green L0, or mock CI.
"""
from __future__ import annotations
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
from gate_guard import FROZEN_MATRIX_SHA256, validate_gate

HERE = Path(__file__).resolve().parent
UTC = dt.timezone.utc
INCIDENT_STATES = [
    "DETECTED", "TRIAGED", "REPRODUCED", "FIX_IN_PR",
    "CI_VERIFIED", "CANARY_DEPLOYED", "TARGETED_PASS",
    "REGRESSION_PASS", "LEARNING_COMMITTED", "VERIFIED_CLOSED"
]
VALID_STATES = set(INCIDENT_STATES) | {"BLOCKED", "REOPENED", "QUARANTINED"}
EVIDENCE_FIELDS = ("p2_sha", "weknora_sha", "oracle_hash", "timestamp_utc", "case_id")


def now_utc():
    return dt.datetime.now(UTC).isoformat(timespec="seconds")


def append_event(path: Path, value: dict):
    """Atomic per-record append with file sync; never replace old verdicts."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())
        os.fsync(fd)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_events(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        result = []
        for i, line in enumerate(f, 1):
            try:
                x = json.loads(line)
                if not isinstance(x, dict):
                    raise ValueError("non-object record")
            except (ValueError, json.JSONDecodeError) as e:
                raise ValueError(f"{path.name}: invalid record {i}: {e}") from e
            result.append(x)
        return result


def load_rules(base=HERE):
    cases = json.loads((base / "cases.json").read_text())["cases"]
    rules = json.loads((base / "learning_rules.json").read_text())
    if hashlib.sha256((base / "cases.json").read_bytes()).hexdigest() != FROZEN_MATRIX_SHA256:
        raise ValueError("frozen case matrix corrupted or changed")
    return {x["id"]: x for x in cases}, rules["rules"]


def sanitized_label(s: str, limit=96) -> str:
    if not isinstance(s, str) or not s or len(s) > limit:
        raise ValueError("missing/invalid label")
    if any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:-" for c in s):
        raise ValueError("label contains unsafe data")
    return s


def failing_incident(state_dir: Path, *, case_id, signature, source_id, metadata=None, base=HERE):
    """Create deduplicated incident, targeted test plan; no secret/error body."""
    cases, rules = load_rules(base)
    case = cases.get(sanitized_label(case_id))
    if case is None:
        raise ValueError("unknown mandatory case")
    signature = sanitized_label(signature)
    source_id = sanitized_label(source_id)
    rule = next((r for r in rules if r["signature"] == signature), None)
    if rule is None:
        # Unknown failure is never silently dropped; require triage.
        related = [case_id]
        extras = ["new_signature_requires_review"]
    else:
        related = [case_id] + [x for x in rule["prioritized_case_ids"] if x != case_id]
        extras = rule["expansion_axes"][:6]
    key = f"{case_id}|{signature}|{source_id}"
    incident = "P2-126-" + hashlib.sha256(key.encode()).hexdigest()[:16]
    previous = [e for e in read_events(state_dir / "incidents.jsonl") if e.get("incident_id") == incident]
    seq = len(previous) + 1
    result = {
        "incident_id": incident, "event_seq": seq, "state": "DETECTED" if not previous else "REOPENED",
        "case_id": case_id, "signature": signature, "source_id": source_id,
        "severity": case["severity"], "detected_utc": now_utc(),
        "targeted_cases": sorted(set(related)), "candidate_axes": extras,
        "requires_human_or_agent_fix": True, "auto_close": False,
        "oracle_change_allowed": False, "destructive_repro_allowed": False,
        "trial_must_requalify": case["severity"] in ("P0","P1"),
    }
    if metadata:
        allowed = {"p2_sha", "weknora_sha", "oracle_hash", "test_run_id", "evidence_sha256"}
        if not set(metadata) <= allowed:
            raise ValueError("unreviewed metadata keys could leak sensitive data")
        result["evidence"] = metadata
    append_event(state_dir / "incidents.jsonl", result)
    if seq == 1:
        append_event(state_dir / "case_proposals.jsonl", {
            "incident_id": incident, "created_utc": result["detected_utc"],
            "case_ids": result["targeted_cases"], "mutation_axes": extras,
            "status": "CANDIDATE_REVIEW_REQUIRED",
            "reason": "red-before-green and negative/adjacent cases mandatory",
        })
    return result


def transition_incident(state_dir: Path, incident_id: str, target: str,
                        evidence: dict | None = None):
    """Advance only with independent evidence. Never fabricate repair progress."""
    if target not in VALID_STATES:
        raise ValueError("invalid target state")
    events = [e for e in read_events(state_dir / "incidents.jsonl")
              if e.get("incident_id") == incident_id]
    if not events:
        raise ValueError("unknown incident")
    last = events[-1]
    prior = last["state"]
    if target not in {"BLOCKED", "REOPENED", "QUARANTINED"}:
        if prior == "REOPENED" and target == "TRIAGED":
            pass
        elif prior not in INCIDENT_STATES or INCIDENT_STATES.index(target) != INCIDENT_STATES.index(prior) + 1:
            raise ValueError("state transition skips required repair/retest stage")
        if not evidence or not evidence.get("artifact_sha256") or not evidence.get("evaluator"):
            raise ValueError("independent, fingerprinted artifact required for every promotion")
        if len(evidence["artifact_sha256"]) != 64 or any(x not in "0123456789abcdef" for x in evidence["artifact_sha256"]):
            raise ValueError("invalid artifact digest")
        sanitized_label(evidence["evaluator"])
    event = {"incident_id": incident_id, "event_seq": len(events) + 1,
             "state": target, "from": prior, "timestamp_utc": now_utc(),
             "evidence": evidence or {}, "case_id": last["case_id"],
             "severity": last["severity"], "signature": last["signature"]}
    append_event(state_dir / "incidents.jsonl", event)
    return event


def slot_id(start: dt.datetime, t: dt.datetime):
    return int((t - start).total_seconds() // 3600)


def parse_time(v):
    x = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    if x.tzinfo is None:
        raise ValueError("naive timestamp is not allowed")
    return x.astimezone(UTC)


def _is_sha256(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _verified_oracle_sources(state_dir, gate):
    """Only a frozen, hash-pinned manifest can establish a source fingerprint."""
    path = state_dir / "frozen-oracle.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        return {}
    raw = path.read_bytes()
    if len(raw) > 2_000_000 or hashlib.sha256(raw).hexdigest() != gate["oracle_sha256"]:
        return {}
    try:
        manifest = json.loads(raw)
        if manifest.get("schema") != "p2-126-frozen-oracle-v1":
            return {}
        items = manifest["sources"]
        if not isinstance(items, list) or not items:
            return {}
        mapping = {}
        for item in items:
            source_id = sanitized_label(item["source_id"])
            digest = item["sha256"]
            if source_id in mapping or not _is_sha256(digest):
                return {}
            mapping[source_id] = digest
        return mapping
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return {}


def _verified_case_artifact(state_dir, entry):
    """Re-hash a private immutable evidence JSON, not merely a claimed digest."""
    digest = entry["evidence_sha256"]
    directory = state_dir / "case-evidence"
    path = directory / (digest + ".json")
    if (directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode & 0o077
            or path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077):
        return False
    raw = path.read_bytes()
    if len(raw) > 2_000_000 or hashlib.sha256(raw).hexdigest() != digest:
        return False
    try:
        proof = json.loads(raw)
        if not isinstance(proof, dict):
            return False
        for key in ("trial_id","case_id","result","layer","source_id","source_sha256",
                    "p2_sha","weknora_sha","runtime_digest","oracle_hash","oracle_sha256","timestamp_utc"):
            if proof.get(key) != entry.get(key):
                return False
        assertions = proof.get("assertions")
        return (isinstance(assertions, list) and bool(assertions)
                and all(isinstance(x, dict) and x.get("passed") is True
                        and isinstance(x.get("name"), str) and x["name"] for x in assertions))
    except (ValueError, TypeError, json.JSONDecodeError):
        return False


def _valid_case_evidence(entry, case, gate, start, now, state_dir, sources):
    """Every mandatory matrix field must bind to signed versions and real bytes."""
    if not isinstance(entry, dict) or any(not entry.get(k) for k in case["evidence_required"]):
        return False
    if (entry.get("trial_id") != gate["trial_id"] or entry.get("result") != "PASS"
            or entry.get("case_id") != case["id"] or entry.get("layer") != case["layer"]
            or entry.get("p2_sha") != gate["p2_sha"]
            or entry.get("weknora_sha") != gate["weknora_sha"]
            or entry.get("runtime_digest") != gate["runtime_digest"]
            or entry.get("oracle_hash") != gate["oracle_sha256"]
            or entry.get("oracle_sha256") != gate["oracle_sha256"]
            or not _is_sha256(entry.get("source_sha256"))
            or not _is_sha256(entry.get("evidence_sha256"))
            or sources.get(entry.get("source_id")) != entry.get("source_sha256")):
        return False
    try:
        stamped = parse_time(entry["timestamp_utc"])
        return start <= stamped <= now and _verified_case_artifact(state_dir, entry)
    except (TypeError, ValueError, AttributeError):
        return False


def _critical_trial_incidents(events, start, now):
    """Reject both new critical incidents and open critical incidents at Gate t0."""
    grouped = {}
    for entry in events:
        if (entry.get("severity") not in ("P0", "P1")
                or not entry.get("incident_id")):
            continue
        try:
            stamp = parse_time(entry.get("detected_utc") or entry.get("timestamp_utc"))
        except (TypeError, ValueError, AttributeError):
            continue
        if stamp <= now:
            grouped.setdefault(entry["incident_id"], []).append((stamp, entry.get("state")))
    invalid = []
    for ident, history in grouped.items():
        history.sort(key=lambda x:x[0])
        before = [state for when,state in history if when <= start]
        open_at_start = bool(before and before[-1] != "VERIFIED_CLOSED")
        new_fault = any(start <= when <= now and state in ("DETECTED","REOPENED")
                        for when,state in history)
        if open_at_start or new_fault:
            invalid.append(ident)
    return sorted(invalid)


def evaluate(state_dir: Path, *, now=None, gate_key=None, base=HERE):
    """Truthful status; 360 hours alone is never a PASS."""
    now = now or dt.datetime.now(UTC)
    gate, reason = validate_gate(state_dir / "gate.json", key_path=gate_key, now=now)
    if not gate:
        return {"verdict":"BLOCKED","mode":"preflight","reason":reason,
                "observed_health_samples":len(read_events(state_dir/"samples.jsonl"))}
    start = parse_time(gate["start_utc"])
    completed_slots = min(360, max(0, slot_id(start, now)))
    cases, _ = load_rules(base)
    l0_by_slot = set()
    l1_by_slot = set()
    for e in read_events(state_dir / "samples.jsonl"):
        try:
            stamp = parse_time(e["sample_time_utc"])
            slot = slot_id(start, stamp)
            if 0 <= slot < completed_slots and e.get("all_healthy") is True and e.get("mode")=="soak" and e.get("trial_id")==gate["trial_id"]:
                l0_by_slot.add(slot)
        except (KeyError, TypeError, ValueError):
            continue
    for e in read_events(state_dir / "business.jsonl"):
        try:
            stamp = parse_time(e["timestamp_utc"])
            slot = slot_id(start, stamp)
            if (0 <= slot < completed_slots and e.get("mode")=="soak"
                    and e.get("trial_id")==gate["trial_id"] and e.get("case_id")=="KB01"
                    and e.get("result")=="PASS" and e.get("oracle_sha256")==gate["oracle_sha256"]):
                l1_by_slot.add(slot)
        except (KeyError, ValueError, TypeError):
            continue
    daily = {}
    for e in read_events(state_dir / "daily.jsonl"):
        try:
            stamp = parse_time(e["timestamp_utc"])
            day = slot_id(start, stamp)//24
            if (start <= stamp <= now and e.get("mode") == "soak"
                    and e.get("trial_id") == gate["trial_id"] and e.get("result") == "PASS"
                    and 0 <= day < 15 and e.get("oracle_sha256")==gate["oracle_sha256"]
                    and len(set(e.get("formats",[])))>=2 and e.get("replay_pass") is True
                    and e.get("incremental_pass") is True):
                daily[day] = True
        except (KeyError, ValueError, TypeError):
            continue
    passed = set()
    oracle_sources = _verified_oracle_sources(state_dir, gate)
    for e in read_events(state_dir / "case_results.jsonl"):
        case = cases.get(e.get("case_id"))
        if case and _valid_case_evidence(e, case, gate, start, now, state_dir, oracle_sources):
            passed.add(e["case_id"])
    missing_cases = sorted(set(cases) - passed)
    missing_l0 = sorted(set(range(completed_slots)) - l0_by_slot)
    missing_l1 = sorted(set(range(completed_slots)) - l1_by_slot)
    incident_events = read_events(state_dir / "incidents.jsonl")
    invalidated = _critical_trial_incidents(incident_events, start, now)
    unresolved = {}
    for e in incident_events:
        if e.get("incident_id"):
            unresolved[e["incident_id"]] = e
    blocking = sorted(k for k,v in unresolved.items() if v.get("state")!="VERIFIED_CLOSED"
                      and v.get("severity") in ("P0","P1"))
    good = (completed_slots==360 and len(l0_by_slot)==360 and len(l1_by_slot)==360
            and len(daily)==15 and not missing_cases and not blocking and not invalidated)
    return {"mode":"soak","trial_id":gate["trial_id"],
            "verdict":"READY_FOR_INDEPENDENT_REVIEW" if good else "BLOCKED",
            "hours_elapsed":round((now-start).total_seconds()/3600,2),
            "completed_slots":completed_slots,"l0_slots":len(l0_by_slot),
            "l1_slots":len(l1_by_slot),"daily_days":len(daily),
            "missing_l0_slots":missing_l0[:24], "missing_l1_slots":missing_l1[:24],
            "missing_case_ids":missing_cases,"unresolved_p0_p1":blocking,
            "trial_invalidated_by":invalidated,
            "note":"READY_FOR_INDEPENDENT_REVIEW is never an automatic PASS"}


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--state-dir",type=Path,required=True)
    sub=p.add_subparsers(dest="command",required=True)
    sub.add_parser("evaluate")
    v=sub.add_parser("incident")
    v.add_argument("--case-id",required=True);v.add_argument("--signature",required=True)
    v.add_argument("--source-id",required=True)
    t=sub.add_parser("transition")
    t.add_argument("--incident-id",required=True)
    t.add_argument("--state",required=True)
    t.add_argument("--artifact-sha256",required=True)
    t.add_argument("--evaluator",required=True)
    args=p.parse_args()
    args.state_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    if args.command=="evaluate":
        res=evaluate(args.state_dir)
    elif args.command=="incident":
        res=failing_incident(args.state_dir,case_id=args.case_id,
                             signature=args.signature,source_id=args.source_id)
    else:
        res=transition_incident(args.state_dir,args.incident_id,args.state,
                               {"artifact_sha256":args.artifact_sha256,"evaluator":args.evaluator})
    print(json.dumps(res,sort_keys=True))
    if args.command=="evaluate" and res["verdict"]=="BLOCKED":
        sys.exit(2)


if __name__=="__main__":
    main()
