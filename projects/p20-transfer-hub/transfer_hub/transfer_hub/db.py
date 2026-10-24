from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .domain import JobState, can_transition


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        with self._connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY,
              job_number INTEGER UNIQUE,
              state TEXT NOT NULL,
              source_type TEXT NOT NULL,
              source TEXT,
              filename TEXT NOT NULL,
              title TEXT,
              engine TEXT NOT NULL,
              options_json TEXT NOT NULL,
              secret_json TEXT NOT NULL DEFAULT '{}',
              progress REAL NOT NULL DEFAULT 0,
              upload_progress REAL NOT NULL DEFAULT 0,
              download_speed INTEGER NOT NULL DEFAULT 0,
              upload_speed INTEGER NOT NULL DEFAULT 0,
              eta_seconds INTEGER,
              original_size INTEGER,
              final_size INTEGER,
              sha256 TEXT,
              artifact_path TEXT,
              result_json TEXT,
              error TEXT,
              retries INTEGER NOT NULL DEFAULT 0,
              cancel_requested INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              completed_at TEXT,
              cleanup_at TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id TEXT,
              type TEXT NOT NULL,
              data_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS job_stages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id TEXT NOT NULL,
              stage TEXT NOT NULL,
              state TEXT NOT NULL,
              sequence INTEGER NOT NULL DEFAULT 0,
              attempt INTEGER NOT NULL DEFAULT 0,
              claim_token TEXT,
              claimed_by TEXT,
              lease_until TEXT,
              last_error TEXT,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              started_at TEXT,
              finished_at TEXT,
              UNIQUE(job_id, stage, sequence),
              FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS delivery_attempts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id TEXT NOT NULL,
              artifact_id INTEGER,
              provider TEXT NOT NULL,
              idempotency_key TEXT NOT NULL,
              provider_task_id TEXT,
              state TEXT NOT NULL,
              result_json TEXT,
              last_error TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(provider, idempotency_key),
              FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS artifacts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id TEXT NOT NULL,
              produced_by_stage TEXT NOT NULL,
              role TEXT NOT NULL,
              path TEXT NOT NULL,
              size INTEGER,
              sha256 TEXT,
              sequence INTEGER NOT NULL DEFAULT 0,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS skills (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, rules_json TEXT NOT NULL,
              config_json TEXT NOT NULL, state TEXT NOT NULL, failure_count INTEGER NOT NULL DEFAULT 0,
              success_count INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1,
              validation_json TEXT NOT NULL DEFAULT '[]', last_verified TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS failure_fingerprints (
              fingerprint TEXT PRIMARY KEY, domain TEXT NOT NULL, source_type TEXT NOT NULL,
              resolver TEXT NOT NULL, engine TEXT NOT NULL, error_class TEXT NOT NULL,
              signature TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 1,
              first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS job_number_sequence (
              singleton INTEGER PRIMARY KEY CHECK(singleton=1), next_value INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS supervisor_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
              failure_event_id INTEGER UNIQUE NOT NULL, status TEXT NOT NULL,
              diagnosis TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS supervisor_messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT, session_key TEXT NOT NULL,
              job_id TEXT, role TEXT NOT NULL, content TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS supervisor_sessions (
              session_key TEXT PRIMARY KEY, codex_session_id TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS supervisor_observations (
              event_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS watch_tasks (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              kind TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              interval_seconds INTEGER NOT NULL,
              config_json TEXT NOT NULL DEFAULT '{}',
              last_item_key TEXT,
              last_item_title TEXT,
              last_job_id TEXT,
              last_run_at TEXT,
              last_outcome TEXT,
              last_message TEXT,
              next_run_at TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS watch_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              watcher_id TEXT NOT NULL,
              outcome TEXT NOT NULL,
              item_key TEXT,
              item_title TEXT,
              job_id TEXT,
              message TEXT,
              details_json TEXT NOT NULL DEFAULT '{}',
              started_at TEXT NOT NULL,
              finished_at TEXT,
              FOREIGN KEY(watcher_id) REFERENCES watch_tasks(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS supervisor_prompts (
              id INTEGER PRIMARY KEY AUTOINCREMENT, session_key TEXT NOT NULL, job_id TEXT,
              status TEXT NOT NULL, question TEXT NOT NULL, choices_json TEXT NOT NULL DEFAULT '[]',
              answer TEXT, created_at TEXT NOT NULL, answered_at TEXT
            );
            CREATE TABLE IF NOT EXISTS supervisor_tasks (
              task_key TEXT PRIMARY KEY, job_id TEXT, state TEXT NOT NULL DEFAULT 'RUNNING',
              current_goal TEXT, confirmed_json TEXT NOT NULL DEFAULT '[]',
              next_steps_json TEXT NOT NULL DEFAULT '[]', pending_prompt_id INTEGER,
              codex_session_id TEXT, previous_session_id TEXT, rotation_reason TEXT,
              turn_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, created_at);
            CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);
            CREATE INDEX IF NOT EXISTS idx_job_stages_claim ON job_stages(stage, state, lease_until, id);
            CREATE INDEX IF NOT EXISTS idx_job_stages_job ON job_stages(job_id, sequence, id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id, produced_by_stage, sequence, id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_stage_unique ON artifacts(job_id, produced_by_stage, role, sequence);
            CREATE INDEX IF NOT EXISTS idx_delivery_attempts_job ON delivery_attempts(job_id, id);
            CREATE INDEX IF NOT EXISTS idx_watch_tasks_due ON watch_tasks(enabled, next_run_at);
            CREATE INDEX IF NOT EXISTS idx_watch_runs_task ON watch_runs(watcher_id, id DESC);
            """)
            columns = {row["name"] for row in c.execute("PRAGMA table_info(jobs)").fetchall()}
            if "job_number" not in columns:
                c.execute("ALTER TABLE jobs ADD COLUMN job_number INTEGER")
            # Assign stable numbers to pre-feature rows once, in creation order.
            legacy = c.execute("SELECT id FROM jobs WHERE job_number IS NULL ORDER BY created_at,id").fetchall()
            current_max = c.execute("SELECT COALESCE(MAX(job_number),0) FROM jobs").fetchone()[0]
            for offset, row in enumerate(legacy, 1):
                c.execute("UPDATE jobs SET job_number=? WHERE id=?", (current_max + offset, row["id"]))
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_job_number ON jobs(job_number)")
            highest = c.execute("SELECT COALESCE(MAX(job_number),0) FROM jobs").fetchone()[0]
            c.execute("INSERT INTO job_number_sequence(singleton,next_value) VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET next_value=MAX(next_value,excluded.next_value)", (highest + 1,))
            if "upload_progress" not in columns:
                c.execute("ALTER TABLE jobs ADD COLUMN upload_progress REAL NOT NULL DEFAULT 0")
            stage_columns = {row["name"] for row in c.execute("PRAGMA table_info(job_stages)").fetchall()}
            if "metadata_json" not in stage_columns:
                c.execute("ALTER TABLE job_stages ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")

    def create_job(self, job: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        row = dict(job)
        options = row.pop("options", {})
        secret = row.pop("secret", {})
        row.update({
            "state": job.get("state", JobState.QUEUED.value),
            "options_json": json.dumps(options, ensure_ascii=False),
            "secret_json": json.dumps(secret, ensure_ascii=False),
            "created_at": now,
            "updated_at": now,
        })
        cols = ",".join(row)
        vals = ",".join("?" for _ in row)
        with self._lock, self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            seq = c.execute("SELECT next_value FROM job_number_sequence WHERE singleton=1").fetchone()[0]
            c.execute("UPDATE job_number_sequence SET next_value=? WHERE singleton=1", (seq + 1,))
            row["job_number"] = seq
            cols = ",".join(row)
            vals = ",".join("?" for _ in row)
            c.execute(f"INSERT INTO jobs ({cols}) VALUES ({vals})", tuple(row.values()))
            c.commit()
        self.event(row["id"], "job.created", {"state": row["state"]})
        return self.get_job(row["id"])

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._decode(row) if row else None

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._decode(r) for r in rows]

    def terminal_jobs(self) -> list[dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute("SELECT * FROM jobs WHERE state IN ('DONE','FAILED','CANCELLED') ORDER BY created_at DESC").fetchall()
        return [self._decode(row) for row in rows]

    def delete_terminal_job(self, job_id: str) -> bool:
        """Delete a job and event history only if it is still terminal."""
        with self._lock, self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["state"] not in {"DONE", "FAILED", "CANCELLED"}:
                c.rollback()
                return False
            c.execute("DELETE FROM events WHERE job_id=?", (job_id,))
            deleted = c.execute(
                "DELETE FROM jobs WHERE id=? AND state IN ('DONE','FAILED','CANCELLED')", (job_id,)
            ).rowcount == 1
            c.commit()
            return deleted

    def next_job(self) -> dict[str, Any] | None:
        with self._connect() as c:
            row = c.execute("SELECT * FROM jobs WHERE state='QUEUED' ORDER BY created_at LIMIT 1").fetchone()
        return self._decode(row) if row else None

    def ensure_stage(self, job_id: str, stage: str, sequence: int = 0, state: str = "READY") -> dict[str, Any]:
        now = utcnow()
        with self._lock, self._connect() as c:
            c.execute(
                "INSERT OR IGNORE INTO job_stages(job_id,stage,state,sequence,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (job_id, stage, state, sequence, now, now),
            )
            row = c.execute(
                "SELECT * FROM job_stages WHERE job_id=? AND stage=? AND sequence=?",
                (job_id, stage, sequence),
            ).fetchone()
        return self._decode_stage(row)

    def list_stages(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute(
                "SELECT * FROM job_stages WHERE job_id=? ORDER BY sequence,id", (job_id,)
            ).fetchall()
        return [self._decode_stage(row) for row in rows]

    def claim_stage(self, stage: str, worker_id: str, lease_seconds: int = 60) -> dict[str, Any] | None:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        lease_until = (now_dt + timedelta(seconds=max(1, lease_seconds))).isoformat()
        token = uuid.uuid4().hex
        with self._lock, self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                """SELECT * FROM job_stages
                   WHERE stage=? AND (state='READY' OR (state='RUNNING' AND lease_until IS NOT NULL AND lease_until<=?))
                   ORDER BY id LIMIT 1""",
                (stage, now),
            ).fetchone()
            if not row:
                c.rollback()
                return None
            c.execute(
                """UPDATE job_stages SET state='RUNNING', attempt=attempt+1, claim_token=?, claimed_by=?,
                   lease_until=?, started_at=COALESCE(started_at,?), updated_at=? WHERE id=?""",
                (token, worker_id, lease_until, now, now, row["id"]),
            )
            claimed = c.execute("SELECT * FROM job_stages WHERE id=?", (row["id"],)).fetchone()
            c.commit()
        return self._decode_stage(claimed)

    def claim_stage_for_job(self, job_id: str, stage: str, worker_id: str, lease_seconds: int = 60) -> dict[str, Any] | None:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        lease_until = (now_dt + timedelta(seconds=max(1, lease_seconds))).isoformat()
        token = uuid.uuid4().hex
        with self._lock, self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("""SELECT * FROM job_stages WHERE job_id=? AND stage=?
                               AND (state='READY' OR (state='RUNNING' AND lease_until IS NOT NULL AND lease_until<=?))
                               ORDER BY id LIMIT 1""", (job_id, stage, now)).fetchone()
            if not row:
                c.rollback()
                return None
            c.execute("""UPDATE job_stages SET state='RUNNING',attempt=attempt+1,claim_token=?,claimed_by=?,
                         lease_until=?,started_at=COALESCE(started_at,?),updated_at=? WHERE id=?""",
                      (token, worker_id, lease_until, now, now, row["id"]))
            out = c.execute("SELECT * FROM job_stages WHERE id=?", (row["id"],)).fetchone()
            c.commit()
        return self._decode_stage(out)

    def extend_stage_lease(self, stage_id: int, claim_token: str, lease_seconds: int = 60) -> bool:
        now_dt = datetime.now(timezone.utc)
        lease_until = (now_dt + timedelta(seconds=max(1, lease_seconds))).isoformat()
        with self._lock, self._connect() as c:
            cur = c.execute(
                "UPDATE job_stages SET lease_until=?,updated_at=? WHERE id=? AND state='RUNNING' AND claim_token=?",
                (lease_until, now_dt.isoformat(), stage_id, claim_token),
            )
        return cur.rowcount == 1

    def finish_stage(self, stage_id: int, claim_token: str, *, success: bool, error: str | None = None) -> bool:
        now = utcnow()
        state = "DONE" if success else "FAILED"
        with self._lock, self._connect() as c:
            cur = c.execute(
                """UPDATE job_stages SET state=?,last_error=?,claim_token=NULL,claimed_by=NULL,lease_until=NULL,
                   finished_at=?,updated_at=? WHERE id=? AND state='RUNNING' AND claim_token=?""",
                (state, None if success else error, now, now, stage_id, claim_token),
            )
        return cur.rowcount == 1

    def release_stage(self, stage_id: int, claim_token: str, error: str | None = None) -> bool:
        with self._lock, self._connect() as c:
            cur = c.execute(
                """UPDATE job_stages SET state='READY',last_error=?,claim_token=NULL,claimed_by=NULL,lease_until=NULL,
                   updated_at=? WHERE id=? AND state='RUNNING' AND claim_token=?""",
                (error, utcnow(), stage_id, claim_token),
            )
        return cur.rowcount == 1


    def update_stage_metadata(self, stage_id: int, claim_token: str, **metadata: Any) -> dict[str, Any] | None:
        with self._lock, self._connect() as c:
            row = c.execute("SELECT metadata_json FROM job_stages WHERE id=? AND state='RUNNING' AND claim_token=?",
                            (stage_id, claim_token)).fetchone()
            if not row:
                return None
            current = json.loads(row["metadata_json"] or "{}")
            current.update(metadata)
            c.execute("UPDATE job_stages SET metadata_json=?,updated_at=? WHERE id=? AND claim_token=?",
                      (json.dumps(current, ensure_ascii=False), utcnow(), stage_id, claim_token))
            out = c.execute("SELECT * FROM job_stages WHERE id=?", (stage_id,)).fetchone()
        return self._decode_stage(out)

    def pause_stage(self, stage_id: int, claim_token: str) -> bool:
        with self._lock, self._connect() as c:
            cur = c.execute("""UPDATE job_stages SET state='PAUSED',claim_token=NULL,claimed_by=NULL,lease_until=NULL,
                               updated_at=? WHERE id=? AND state='RUNNING' AND claim_token=?""",
                            (utcnow(), stage_id, claim_token))
        return cur.rowcount == 1

    def resume_stage(self, job_id: str, stage: str) -> bool:
        with self._lock, self._connect() as c:
            cur = c.execute("UPDATE job_stages SET state='READY',updated_at=? WHERE job_id=? AND stage=? AND state='PAUSED'",
                            (utcnow(), job_id, stage))
        return cur.rowcount == 1

    def retry_failed_stage(self, job_id: str) -> dict[str, Any] | None:
        now = utcnow()
        with self._lock, self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM job_stages WHERE job_id=? AND state='FAILED' ORDER BY id DESC LIMIT 1",
                            (job_id,)).fetchone()
            if not row:
                c.rollback()
                return None
            c.execute("""UPDATE job_stages SET state='READY',claim_token=NULL,claimed_by=NULL,lease_until=NULL,
                         last_error=NULL,finished_at=NULL,updated_at=? WHERE id=?""", (now, row["id"]))
            out = c.execute("SELECT * FROM job_stages WHERE id=?", (row["id"],)).fetchone()
            c.commit()
        return self._decode_stage(out)

    def cancel_active_stages(self, job_id: str) -> int:
        with self._lock, self._connect() as c:
            cur = c.execute("""UPDATE job_stages SET state='CANCELLED',claim_token=NULL,claimed_by=NULL,lease_until=NULL,
                               updated_at=?,finished_at=? WHERE job_id=? AND state IN ('READY','RUNNING','PAUSED')""",
                            (utcnow(), utcnow(), job_id))
        return cur.rowcount

    def reset_pipeline(self, job_id: str) -> None:
        with self._lock, self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM delivery_attempts WHERE job_id=?", (job_id,))
            c.execute("DELETE FROM artifacts WHERE job_id=?", (job_id,))
            c.execute("DELETE FROM job_stages WHERE job_id=?", (job_id,))
            c.commit()

    def get_delivery_attempt(self, job_id: str, idempotency_key: str) -> dict[str, Any] | None:
        with self._connect() as c:
            row = c.execute("SELECT * FROM delivery_attempts WHERE job_id=? AND idempotency_key=?",
                            (job_id, idempotency_key)).fetchone()
        return self._decode_delivery_attempt(row) if row else None

    def upsert_delivery_attempt(self, job_id: str, provider: str, idempotency_key: str, *,
                                artifact_id: int | None = None, provider_task_id: str | None = None,
                                state: str = 'STARTING', result: dict[str, Any] | None = None,
                                error: str | None = None) -> dict[str, Any]:
        now = utcnow()
        with self._lock, self._connect() as c:
            c.execute("""INSERT INTO delivery_attempts(job_id,artifact_id,provider,idempotency_key,provider_task_id,state,result_json,last_error,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(provider,idempotency_key) DO UPDATE SET
                         artifact_id=COALESCE(excluded.artifact_id,delivery_attempts.artifact_id),
                         provider_task_id=COALESCE(excluded.provider_task_id,delivery_attempts.provider_task_id),
                         state=excluded.state,result_json=COALESCE(excluded.result_json,delivery_attempts.result_json),
                         last_error=excluded.last_error,updated_at=excluded.updated_at""",
                      (job_id, artifact_id, provider, idempotency_key, provider_task_id, state,
                       json.dumps(result, ensure_ascii=False) if result is not None else None, error, now, now))
            row = c.execute("SELECT * FROM delivery_attempts WHERE provider=? AND idempotency_key=?",
                            (provider, idempotency_key)).fetchone()
        return self._decode_delivery_attempt(row)

    def add_artifact(self, job_id: str, produced_by_stage: str, role: str, path: str, *,
                     size: int | None = None, sha256: str | None = None, sequence: int = 0,
                     metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock, self._connect() as c:
            cur = c.execute(
                """INSERT INTO artifacts(job_id,produced_by_stage,role,path,size,sha256,sequence,metadata_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(job_id,produced_by_stage,role,sequence) DO UPDATE SET
                     path=excluded.path,size=excluded.size,sha256=excluded.sha256,metadata_json=excluded.metadata_json""",
                (job_id, produced_by_stage, role, path, size, sha256, sequence,
                 json.dumps(metadata or {}, ensure_ascii=False), utcnow()),
            )
            row = c.execute("SELECT * FROM artifacts WHERE job_id=? AND produced_by_stage=? AND role=? AND sequence=?",
                            (job_id, produced_by_stage, role, sequence)).fetchone()
        out = dict(row)
        out["metadata"] = json.loads(out.pop("metadata_json") or "{}")
        return out

    def delete_artifacts(self, job_id: str, produced_by_stage: str) -> list[dict[str, Any]]:
        rows = self.list_artifacts(job_id, produced_by_stage)
        with self._lock, self._connect() as c:
            c.execute("DELETE FROM artifacts WHERE job_id=? AND produced_by_stage=?", (job_id, produced_by_stage))
        return rows

    def list_artifacts(self, job_id: str, produced_by_stage: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as c:
            if produced_by_stage:
                rows = c.execute(
                    "SELECT * FROM artifacts WHERE job_id=? AND produced_by_stage=? ORDER BY sequence,id",
                    (job_id, produced_by_stage),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM artifacts WHERE job_id=? ORDER BY id", (job_id,)
                ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            out.append(item)
        return out

    def latest_stage(self, job_id: str, stage: str | None = None) -> dict[str, Any] | None:
        with self._connect() as c:
            if stage:
                row = c.execute("SELECT * FROM job_stages WHERE job_id=? AND stage=? ORDER BY sequence DESC,id DESC LIMIT 1",
                                (job_id, stage)).fetchone()
            else:
                row = c.execute("SELECT * FROM job_stages WHERE job_id=? ORDER BY id DESC LIMIT 1",
                                (job_id,)).fetchone()
        return self._decode_stage(row) if row else None

    def stage_queue_position(self, stage_id: int) -> int | None:
        with self._connect() as c:
            row = c.execute("SELECT stage,state,id FROM job_stages WHERE id=?", (stage_id,)).fetchone()
            if not row or row["state"] != "READY":
                return None
            ahead = c.execute(
                "SELECT COUNT(*) FROM job_stages WHERE stage=? AND state='READY' AND id<?",
                (row["stage"], row["id"]),
            ).fetchone()[0]
        return int(ahead) + 1

    def active_stage_count(self, stage: str | None = None) -> int:
        with self._connect() as c:
            if stage:
                row = c.execute("SELECT COUNT(*) FROM job_stages WHERE stage=? AND state IN ('READY','RUNNING','PAUSED')",
                                (stage,)).fetchone()
            else:
                row = c.execute("SELECT COUNT(*) FROM job_stages WHERE state IN ('READY','RUNNING','PAUSED')").fetchone()
        return int(row[0])

    def jobs_in_states(self, states: list[str], limit: int = 1000) -> list[dict[str, Any]]:
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        with self._connect() as c:
            rows = c.execute(
                f"SELECT * FROM jobs WHERE state IN ({placeholders}) ORDER BY created_at LIMIT ?",
                (*states, limit),
            ).fetchall()
        return [self._decode(r) for r in rows]

    def expired_ready_jobs(self, now_iso: str, limit: int = 1000) -> list[dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute(
                "SELECT * FROM jobs WHERE state='READY' AND cleanup_at IS NOT NULL AND cleanup_at<=? ORDER BY cleanup_at LIMIT ?",
                (now_iso, limit),
            ).fetchall()
        return [self._decode(r) for r in rows]

    def claim_expired_ready(self, job_id: str, now_iso: str) -> bool:
        with self._lock, self._connect() as c:
            return c.execute(
                "UPDATE jobs SET state='CLEANUP',updated_at=? "
                "WHERE id=? AND state='READY' AND cleanup_at IS NOT NULL AND cleanup_at<=?",
                (utcnow(), job_id, now_iso),
            ).rowcount == 1

    def clear_failed_expiry(self, job_id: str, cleanup_at: str) -> bool:
        with self._lock, self._connect() as c:
            return c.execute(
                "UPDATE jobs SET cleanup_at=NULL,updated_at=? "
                "WHERE id=? AND state IN ('FAILED','CANCELLED') AND cleanup_at=?",
                (utcnow(), job_id, cleanup_at),
            ).rowcount == 1

    def expired_failed_jobs(self, now_iso: str, limit: int = 1000) -> list[dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute(
                "SELECT * FROM jobs WHERE state IN ('FAILED','CANCELLED') AND cleanup_at IS NOT NULL AND cleanup_at<=? ORDER BY cleanup_at LIMIT ?",
                (now_iso, limit),
            ).fetchall()
        return [self._decode(r) for r in rows]

    def update(self, job_id: str, **fields: Any) -> dict[str, Any]:
        if not fields:
            return self.get_job(job_id)
        fields["updated_at"] = utcnow()
        if "options" in fields:
            fields["options_json"] = json.dumps(fields.pop("options"), ensure_ascii=False)
        if "secret" in fields:
            fields["secret_json"] = json.dumps(fields.pop("secret"), ensure_ascii=False)
        if "result" in fields:
            fields["result_json"] = json.dumps(fields.pop("result"), ensure_ascii=False)
        sets = ",".join(f"{k}=?" for k in fields)
        with self._lock, self._connect() as c:
            c.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))
        return self.get_job(job_id)

    def transition(self, job_id: str, state: JobState, **fields: Any) -> dict[str, Any]:
        job = self.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        if not can_transition(job["state"], state):
            raise ValueError(f"invalid transition {job['state']} -> {state}")
        if state in {JobState.DONE, JobState.FAILED, JobState.CANCELLED}:
            fields.setdefault("completed_at", utcnow())
        if state in {JobState.READY, JobState.FAILED, JobState.CANCELLED} and state.value != job["state"]:
            from .retention import deadline
            fields["cleanup_at"] = deadline()
        out = self.update(job_id, state=state.value, **fields)
        self.event(job_id, "job.state", {"state": state.value})
        return out

    def event(self, job_id: str | None, event_type: str, data: dict[str, Any]) -> int:
        with self._lock, self._connect() as c:
            cur = c.execute(
                "INSERT INTO events(job_id,type,data_json,created_at) VALUES(?,?,?,?)",
                (job_id, event_type, json.dumps(data, ensure_ascii=False), utcnow()),
            )
            return int(cur.lastrowid)

    def recent_events(self, job_id: str, limit: int = 30, event_type: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as c:
            if event_type:
                rows = c.execute(
                    "SELECT * FROM events WHERE job_id=? AND type=? ORDER BY id DESC LIMIT ?",
                    (job_id, event_type, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM events WHERE job_id=? ORDER BY id DESC LIMIT ?",
                    (job_id, limit),
                ).fetchall()
        out = [{**dict(r), "data": json.loads(r["data_json"])} for r in rows]
        out.reverse()
        return out

    def seed_watch_task(self, task: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        with self._lock, self._connect() as c:
            c.execute(
                """INSERT INTO watch_tasks(
                   id,name,kind,enabled,interval_seconds,config_json,next_run_at,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name,kind=excluded.kind,interval_seconds=excluded.interval_seconds,
                     config_json=excluded.config_json,updated_at=excluded.updated_at""",
                (task["id"], task["name"], task["kind"], 1 if task.get("enabled", True) else 0,
                 int(task.get("interval_seconds", 300)), json.dumps(task.get("config") or {}, ensure_ascii=False),
                 task.get("next_run_at") or now, now, now),
            )
        return self.get_watch_task(task["id"])

    def get_watch_task(self, watcher_id: str) -> dict[str, Any] | None:
        with self._connect() as c:
            row = c.execute("SELECT * FROM watch_tasks WHERE id=?", (watcher_id,)).fetchone()
        return self._decode_watch_task(row) if row else None

    def list_watch_tasks(self) -> list[dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute("SELECT * FROM watch_tasks ORDER BY name,id").fetchall()
        return [self._decode_watch_task(row) for row in rows]

    def due_watch_tasks(self, now_iso: str) -> list[dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute(
                "SELECT * FROM watch_tasks WHERE enabled=1 AND (next_run_at IS NULL OR next_run_at<=?) ORDER BY next_run_at,id",
                (now_iso,),
            ).fetchall()
        return [self._decode_watch_task(row) for row in rows]

    def update_watch_task(self, watcher_id: str, **fields: Any) -> dict[str, Any]:
        if not fields:
            result = self.get_watch_task(watcher_id)
            if result is None:
                raise KeyError(watcher_id)
            return result
        if "config" in fields:
            fields["config_json"] = json.dumps(fields.pop("config"), ensure_ascii=False)
        fields["updated_at"] = utcnow()
        sets = ",".join(f"{key}=?" for key in fields)
        with self._lock, self._connect() as c:
            cur = c.execute(f"UPDATE watch_tasks SET {sets} WHERE id=?", (*fields.values(), watcher_id))
        if cur.rowcount != 1:
            raise KeyError(watcher_id)
        return self.get_watch_task(watcher_id)

    def start_watch_run(self, watcher_id: str) -> dict[str, Any]:
        now = utcnow()
        with self._lock, self._connect() as c:
            cur = c.execute(
                "INSERT INTO watch_runs(watcher_id,outcome,started_at) VALUES(?,?,?)",
                (watcher_id, "RUNNING", now),
            )
            row = c.execute("SELECT * FROM watch_runs WHERE id=?", (cur.lastrowid,)).fetchone()
        return self._decode_watch_run(row)

    def finish_watch_run(self, run_id: int, *, outcome: str, item_key: str | None = None,
                         item_title: str | None = None, job_id: str | None = None,
                         message: str | None = None, details: dict[str, Any] | None = None) -> dict[str, Any]:
        finished = utcnow()
        with self._lock, self._connect() as c:
            c.execute(
                """UPDATE watch_runs SET outcome=?,item_key=?,item_title=?,job_id=?,message=?,details_json=?,finished_at=?
                   WHERE id=?""",
                (outcome, item_key, item_title, job_id, message, json.dumps(details or {}, ensure_ascii=False), finished, run_id),
            )
            row = c.execute("SELECT * FROM watch_runs WHERE id=?", (run_id,)).fetchone()
            if row:
                c.execute(
                    "DELETE FROM watch_runs WHERE watcher_id=? AND id NOT IN (SELECT id FROM watch_runs WHERE watcher_id=? ORDER BY id DESC LIMIT 500)",
                    (row["watcher_id"], row["watcher_id"]),
                )
        if not row:
            raise KeyError(run_id)
        return self._decode_watch_run(row)

    def list_watch_runs(self, watcher_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as c:
            if watcher_id:
                rows = c.execute("SELECT * FROM watch_runs WHERE watcher_id=? ORDER BY id DESC LIMIT ?", (watcher_id, limit)).fetchall()
            else:
                rows = c.execute("SELECT * FROM watch_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._decode_watch_run(row) for row in rows]

    def get_watch_run(self, run_id: int) -> dict[str, Any] | None:
        with self._connect() as c:
            row = c.execute("SELECT * FROM watch_runs WHERE id=?", (run_id,)).fetchone()
        return self._decode_watch_run(row) if row else None

    # Legacy supervisor_* tables above are intentionally retained so 0.5.0 can
    # open an existing 0.4.x database without destroying historical data.
    # Runtime AI accessors were removed in 0.5.0.

    def events_after(self, after_id: int = 0, job_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as c:
            if job_id:
                rows = c.execute(
                    "SELECT * FROM events WHERE id>? AND job_id=? ORDER BY id LIMIT ?",
                    (after_id, job_id, limit),
                ).fetchall()
            else:
                rows = c.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (after_id, limit)).fetchall()
        return [{**dict(r), "data": json.loads(r["data_json"])} for r in rows]

    def save_skill(self, skill: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        row = {"id": skill["id"], "name": skill["name"],
               "rules_json": skill.get("rules_json") or json.dumps(skill.get("rules", {})),
               "config_json": skill.get("config_json") or json.dumps(skill.get("config", {})),
               "state": skill["state"], "failure_count": skill.get("failure_count", 0),
               "success_count": skill.get("success_count", 0), "version": skill.get("version", 1),
               "validation_json": skill.get("validation_json") or json.dumps(skill.get("validation_history", [])),
               "last_verified": skill.get("last_verified"), "created_at": skill.get("created_at") or now,
               "updated_at": now}
        with self._lock, self._connect() as c:
            c.execute("""INSERT INTO skills(id,name,rules_json,config_json,state,failure_count,success_count,version,validation_json,last_verified,created_at,updated_at)
              VALUES(:id,:name,:rules_json,:config_json,:state,:failure_count,:success_count,:version,:validation_json,:last_verified,:created_at,:updated_at)
              ON CONFLICT(id) DO UPDATE SET name=excluded.name,rules_json=excluded.rules_json,config_json=excluded.config_json,state=excluded.state,failure_count=excluded.failure_count,success_count=excluded.success_count,version=excluded.version,validation_json=excluded.validation_json,last_verified=excluded.last_verified,updated_at=excluded.updated_at""", row)
        return self.get_skill(skill["id"])

    def get_skill(self, skill_id: str) -> dict[str, Any] | None:
        with self._connect() as c:
            row = c.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
        return self._decode_skill(row) if row else None

    def list_skills(self, state: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute("SELECT * FROM skills" + (" WHERE state=?" if state else "") + " ORDER BY updated_at DESC", (state,) if state else ()).fetchall()
        return [self._decode_skill(row) for row in rows]

    def record_fingerprint(self, row: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        with self._lock, self._connect() as c:
            c.execute("""INSERT INTO failure_fingerprints(fingerprint,domain,source_type,resolver,engine,error_class,signature,count,first_seen,last_seen)
              VALUES(:fingerprint,:domain,:source_type,:resolver,:engine,:error_class,:signature,1,:now,:now)
              ON CONFLICT(fingerprint) DO UPDATE SET count=count+1,last_seen=:now""", {**row, "now": now})
            result = c.execute("SELECT * FROM failure_fingerprints WHERE fingerprint=?", (row["fingerprint"],)).fetchone()
        return dict(result)

    def list_fingerprints(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute("SELECT * FROM failure_fingerprints ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def get_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        with self._connect() as c:
            row = c.execute("SELECT * FROM failure_fingerprints WHERE fingerprint=?", (fingerprint,)).fetchone()
        return dict(row) if row else None


    @staticmethod
    def _decode_watch_task(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["config"] = json.loads(out.pop("config_json") or "{}")
        out["enabled"] = bool(out["enabled"])
        return out

    @staticmethod
    def _decode_watch_run(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["details"] = json.loads(out.pop("details_json") or "{}")
        return out

    @staticmethod
    def _decode_stage(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["metadata"] = json.loads(out.pop("metadata_json") or "{}")
        return out

    @staticmethod
    def _decode_delivery_attempt(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["result"] = json.loads(out.pop("result_json")) if out.get("result_json") else None
        out.pop("result_json", None)
        return out

    @staticmethod
    def _decode_skill(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["rules"] = json.loads(out.pop("rules_json"))
        out["config"] = json.loads(out.pop("config_json"))
        out["validation_history"] = json.loads(out.pop("validation_json"))
        return out

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["options"] = json.loads(out.pop("options_json") or "{}")
        out["secret"] = json.loads(out.pop("secret_json") or "{}")
        out["result"] = json.loads(out.pop("result_json")) if out.get("result_json") else None
        out.pop("result_json", None)
        out["cancel_requested"] = bool(out["cancel_requested"])
        return out
