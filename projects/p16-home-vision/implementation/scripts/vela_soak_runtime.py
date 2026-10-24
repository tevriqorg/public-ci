"""Candidate G1 evidence/continuous-loop primitives; no deployment or day credit."""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
import sqlite3
import signal
import subprocess
from contextlib import contextmanager
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
        self.index_path = self.root / "runs.sqlite3"
        # Run files are authority. Rebuild under the writer lock after a crash;
        # append uses indexed lookups instead of rehashing all prior runs.
        with (self.root / ".evidence-writer.lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            prior = self.records()
            with sqlite3.connect(self.index_path) as db:
                db.execute("CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, record TEXT NOT NULL)")
                db.execute("DELETE FROM runs")
                db.executemany("INSERT INTO runs VALUES (?, ?)",
                               [(r["run_id"], json.dumps(r)) for r in prior])

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
            with sqlite3.connect(self.index_path) as db:
                db.execute("BEGIN IMMEDIATE")
                name = self._append_locked(record, db)
                db.execute("INSERT INTO runs VALUES (?, ?)",
                           (record["run_id"], json.dumps(record)))
                return name

    def _append_locked(self, record, db):
        required = {"run_id", "source_sha", "scenario_id", "seed", "status", "timestamp_utc"}
        if not required <= record.keys() or record["status"] not in ("PASS", "FAIL", "BLOCKED"):
            raise ValueError("INVALID_RUN_RECORD")
        if db.execute("SELECT 1 FROM runs WHERE run_id = ?", (record["run_id"],)).fetchone():
            raise ValueError("DUPLICATE_RUN_ID")
        recovery = record.get("recovery_of")
        if recovery:
            row = db.execute("SELECT record FROM runs WHERE run_id = ?", (recovery,)).fetchone()
            original = json.loads(row[0]) if row else None
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
    with cycle_lock(store.root):
        return _run_cycle(catalog, handlers, store, source_sha=source_sha, guard=guard, clock=clock)


def _run_cycle(catalog, handlers, store, *, source_sha, guard, clock=None):
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


@contextmanager
def cycle_lock(root):
    """Hold across the entire cycle, not only an individual evidence append."""
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    with (path / ".cycle.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def command_handler(argv, *, cwd, timeout_seconds):
    """Run an approved test command without shell; timeout kills its process group.

    Child output is deliberately not collected here: test-specific evidence must
    redact and bound artifacts. Exit status alone does not establish an oracle.
    """
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(v, str) for v in argv):
        raise ValueError("INVALID_COMMAND")
    if timeout_seconds <= 0:
        raise ValueError("INVALID_TIMEOUT")
    def execute(scenario):
        started = time.monotonic()
        process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        timed_out = False
        try:
            code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            code = None
        finally:
            # Clean surviving descendants even if their parent exited normally.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        return {"status": "FAIL" if timed_out or code != 0 else "PASS",
                "exit_code": code, "timed_out": timed_out,
                "elapsed_seconds": round(time.monotonic() - started, 3)}
    return execute
