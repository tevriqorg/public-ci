"""Candidate G1 evidence/continuous-loop primitives; no deployment or day credit."""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
import sqlite3
import signal
import subprocess
import sys
from contextlib import contextmanager
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


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
        with cycle_lock(self.root):
            with (self.root / ".evidence-writer.lock").open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._rebuild()

    def _rebuild(self):
        prior = self.records()
        with sqlite3.connect(self.index_path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, record TEXT NOT NULL)")
            db.execute("DELETE FROM runs")
            db.executemany("INSERT INTO runs VALUES (?, ?)",
                           [(r["run_id"], json.dumps(r)) for r in prior])
        (self.root / ".publication-pending").unlink(missing_ok=True)

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
            marker = self.root / ".publication-pending"
            if marker.exists():
                self._rebuild()
            # Durable intent precedes publication; surviving instances reconcile
            # under the same writer lock before any indexed lookup.
            with marker.open("w") as pending:
                pending.flush()
                os.fsync(pending.fileno())
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            with sqlite3.connect(self.index_path) as db:
                db.execute("BEGIN IMMEDIATE")
                name = self._append_locked(record, db)
                db.execute("INSERT INTO runs VALUES (?, ?)",
                           (record["run_id"], json.dumps(record)))
            marker.unlink()
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


def record_daily_cycle(root, *, scheduled_start, observed_day, run_id,
                       complete, observed_at=None):
    """Persist one daily attempt without erasing missed calendar-day history.

    A later catch-up attempt is retained in daily_attempts, while an originally
    missed date remains MISSING in daily_schedule and cannot be turned into a
    credited PASS retroactively.
    """
    try:
        start = date.fromisoformat(scheduled_start)
        day = date.fromisoformat(observed_day)
    except (TypeError, ValueError) as exc:
        raise ValueError("INVALID_SCHEDULE_DATE") from exc
    if start.isoformat() != scheduled_start or day.isoformat() != observed_day:
        raise ValueError("INVALID_SCHEDULE_DATE")
    singapore = ZoneInfo("Asia/Singapore")
    now = datetime.now(singapore)
    try:
        attempt_time = datetime.fromisoformat(observed_at) if observed_at else now
    except (TypeError, ValueError) as exc:
        raise ValueError("INVALID_ATTEMPT_TIME") from exc
    if attempt_time.tzinfo is None:
        raise ValueError("INVALID_ATTEMPT_TIME")
    execution_day = attempt_time.astimezone(singapore).date()
    if (day < start or day > execution_day or execution_day > now.date()):
        raise ValueError("SCHEDULE_DAY_OUT_OF_RANGE")
    if not isinstance(run_id, str) or not run_id or not isinstance(complete, bool):
        raise ValueError("INVALID_DAILY_ATTEMPT")
    when = attempt_time.isoformat()
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path / "daily-ledger.sqlite3") as db:
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS daily_schedule (day TEXT PRIMARY KEY, state TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS daily_attempts (run_id TEXT PRIMARY KEY, day TEXT NOT NULL, observed_at TEXT NOT NULL, outcome TEXT NOT NULL)")
        cursor = start
        while cursor <= execution_day:
            # Finalize elapsed dates as MISSING. Leave today's slot open when
            # this attempt is only catching up an older date; a later on-date
            # run may still earn that date's outcome.
            if cursor < execution_day:
                db.execute("INSERT OR IGNORE INTO daily_schedule VALUES (?, 'MISSING')",
                           (cursor.isoformat(),))
            elif cursor == day:
                state = "PASS" if complete else "FAIL"
                db.execute("INSERT OR IGNORE INTO daily_schedule VALUES (?, ?)",
                           (cursor.isoformat(), state))
            cursor += timedelta(days=1)
        outcome = "PASS" if complete else "FAIL"
        try:
            db.execute("INSERT INTO daily_attempts VALUES (?, ?, ?, ?)",
                       (run_id, day.isoformat(), when, outcome))
        except sqlite3.IntegrityError as exc:
            raise ValueError("DUPLICATE_DAILY_RUN_ID") from exc
        db.commit()
    with sqlite3.connect(path / "daily-ledger.sqlite3") as db:
        states = db.execute("SELECT day, state FROM daily_schedule ORDER BY day").fetchall()
    return {"schedule": states, "attempt": (run_id, day.isoformat(), outcome)}


def run_cycle(catalog, handlers, store, *, source_sha, guard, clock=None):
    with cycle_lock(store.root):
        return _run_cycle(catalog, handlers, store, source_sha=source_sha, guard=guard, clock=clock)


def run_scenario(catalog, scenario_id, handlers, store, *, source_sha, guard, clock=None):
    """Validate the frozen catalog, then execute exactly one selected scenario."""
    from vela_g0_validate import inspect
    if inspect(catalog):
        raise ValueError("INVALID_CATALOG")
    selected = [s for s in catalog["scenarios"] if s.get("id") == scenario_id]
    if len(selected) != 1:
        raise ValueError("UNKNOWN_SCENARIO")
    with cycle_lock(store.root):
        subset = {**catalog, "scenarios": selected}
        return _run_cycle(subset, handlers, store, source_sha=source_sha,
                          guard=guard, clock=clock, catalog_validated=True)


def _run_cycle(catalog, handlers, store, *, source_sha, guard, clock=None,
               catalog_validated=False):
    """Execute registered scenarios; unknown handlers are BLOCKED, never skipped."""
    if not catalog_validated:
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


def command_handler(argv, *, cwd, timeout_seconds, forward_stderr=False):
    """Run an approved test command without shell; timeout kills its process group.

    Child output is deliberately not collected here: test-specific evidence must
    redact and bound artifacts. Exit status alone does not establish an oracle.
    """
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(v, str) for v in argv):
        raise ValueError("INVALID_COMMAND")
    if timeout_seconds <= 0:
        raise ValueError("INVALID_TIMEOUT")
    command = list(argv)
    # Pytest reports skipped tests with exit code 0. A soak PASS must mean each
    # selected injection/oracle actually ran, so scenario wrappers fail closed
    # on any skip (including fixture/setup skips and xfails).
    if len(command) >= 3 and command[1:3] == ["-m", "pytest"]:
        command[3:3] = ["-p", "tests.soak_fail_on_skip"]
    def execute(scenario):
        started = time.monotonic()
        child_env = os.environ.copy()
        child_env["P16_SOAK_WRAPPER_CHILD"] = "1"
        process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=sys.stderr if forward_stderr else subprocess.DEVNULL,
                                   stderr=None if forward_stderr else subprocess.DEVNULL,
                                   env=child_env,
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
