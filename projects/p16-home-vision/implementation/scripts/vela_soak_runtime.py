"""Candidate G1 evidence/continuous-loop primitives; no deployment or day credit."""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class EvidenceStore:
    """Content-addressed immutable runs; index is reconstructible from run files."""
    def __init__(self, root):
        self.root = Path(root)
        self.runs = self.root / "runs"
        self.runs.mkdir(parents=True, exist_ok=True)

    def records(self):
        records = []
        for path in self.runs.glob("*.json"):
            raw = path.read_bytes()
            record = json.loads(raw)
            if digest(record) != path.stem:
                raise ValueError("EVIDENCE_HASH_MISMATCH")
            records.append(record)
        return records

    def append(self, record):
        with (self.root / ".evidence-writer.lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self._append_locked(record)

    def _append_locked(self, record):
        required = {"run_id", "source_sha", "scenario_id", "seed", "status", "timestamp_utc"}
        if not required <= record.keys() or record["status"] not in ("PASS", "FAIL", "BLOCKED"):
            raise ValueError("INVALID_RUN_RECORD")
        prior = self.records()
        if any(r["run_id"] == record["run_id"] for r in prior):
            raise ValueError("DUPLICATE_RUN_ID")
        recovery = record.get("recovery_of")
        if recovery:
            original = next((r for r in prior if r["run_id"] == recovery), None)
            if original is None or original["status"] != "FAIL":
                raise ValueError("RECOVERY_WITHOUT_ORIGINAL_FAIL")
            if (original["scenario_id"], original["seed"]) != (record["scenario_id"], record["seed"]):
                raise ValueError("RECOVERY_REPLAY_MISMATCH")
        name = digest(record)
        temporary = self.runs / ("." + uuid.uuid4().hex + ".tmp")
        with temporary.open("xb") as f:
            f.write(json.dumps(record, sort_keys=True, separators=(",", ":")).encode())
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(temporary, self.runs / (name + ".json"))
            directory = os.open(self.runs, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return name


def run_cycle(catalog, handlers, store, *, source_sha, guard, clock=None):
    """Execute registered scenarios; unknown handlers are BLOCKED, never skipped."""
    from vela_g0_validate import inspect
    if inspect(catalog):
        raise ValueError("INVALID_CATALOG")
    now = clock or (lambda: datetime.now(timezone.utc))
    cycle_id = uuid.uuid4().hex
    outcomes = []
    for scenario in catalog["scenarios"]:
        record = {"run_id": cycle_id + "-" + scenario["id"], "source_sha": source_sha,
                  "scenario_id": scenario["id"], "seed": scenario["seed"],
                  "timestamp_utc": now().isoformat(), "status": "BLOCKED"}
        try:
            guard()
        except Exception as exc:
            record["reason"] = "RESOURCE_GUARD:" + type(exc).__name__
            store.append(record)
            outcomes.append(record)
            break
        handler = handlers.get(scenario["id"])
        if handler is None:
            record["reason"] = "SCENARIO_NOT_IMPLEMENTED"
        else:
            try:
                result = handler(scenario)
                if not isinstance(result, dict) or result.get("status") not in ("PASS", "FAIL"):
                    raise ValueError("INVALID_HANDLER_RESULT")
                record.update(status=result["status"], result=result)
            except Exception as exc:
                # Preserve a red result and continue independent checks; never retry-to-green.
                record.update(status="FAIL", reason="HANDLER_EXCEPTION:" + type(exc).__name__)
        store.append(record)
        outcomes.append(record)
    return {"cycle_id": cycle_id, "results": outcomes,
            "complete": len(outcomes) == len(catalog["scenarios"]) and
                        all(r["status"] == "PASS" for r in outcomes),
            "day_credit": False, "gate": "G1_CANDIDATE_ONLY"}


def continuous(cycle, *, stop_requested, interval_seconds=60, wait=time.sleep):
    """Persistent process; stop control checked before each bounded cycle."""
    if interval_seconds < 1:
        raise ValueError("INVALID_CYCLE_INTERVAL")
    while not stop_requested():
        cycle()
        if not stop_requested():
            wait(interval_seconds)
