"""P20 local retention policy: 48 h after READY/FAILED; no QQ-cloud revocation."""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

RETENTION_SECONDS = 48 * 60 * 60


def deadline(base: str | None = None) -> str:
    current = datetime.fromisoformat(base) if base else datetime.now(timezone.utc)
    return (current + timedelta(seconds=RETENTION_SECONDS)).isoformat()


def _safe_job_artifacts(job: dict, data_dir: Path) -> None:
    """Only delete P20-owned work and uploads, never VPS-origin input or unrelated paths."""
    data_dir = data_dir.resolve()
    work_root = (data_dir / "jobs").resolve()
    candidate = work_root / job["id"]
    resolved = candidate.resolve()
    if resolved.parent != work_root:
        raise ValueError("unsafe P20 jobs path")
    if candidate.is_symlink():
        candidate.unlink()
    elif candidate.is_dir():
        shutil.rmtree(candidate)
    local = job.get("options", {}).get("local_path")
    if local:
        uploads_root = (data_dir / "uploads").resolve()
        candidate = Path(local)
        resolved = candidate.resolve()
        if resolved.parent == uploads_root:
            candidate.unlink(missing_ok=True)


def sweep_jobs(store, data_dir: Path, *, now_iso: str | None = None) -> dict[str, int]:
    """Crash-recoverable READY cleanup, FAILED payload reclamation, metadata retained."""
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    result = {"ready": 0, "recovered": 0, "failed": 0}
    for job in store.expired_ready_jobs(now_iso):
        if not store.claim_expired_ready(job["id"], now_iso):
            continue
        job["state"] = "CLEANUP"
        store.event(job["id"], "job.ttl_expired", {"cleanup_at": job["cleanup_at"]})
    for job in store.jobs_in_states(["CLEANUP"]):
        # Only sweeper-owned or abandoned terminal cleanup, not an in-progress ACK.
        if not job.get("cleanup_at") or job["cleanup_at"] > now_iso:
            continue
        _safe_job_artifacts(job, data_dir)
        from .domain import JobState
        store.transition(job["id"], JobState.DONE, progress=100)
        result["ready"] += 1
    for job in store.expired_failed_jobs(now_iso):
        # Never delete live retries. Recheck before payload reclamation.
        current = store.get_job(job["id"])
        if not current or current["state"] not in {"FAILED", "CANCELLED"} or current.get("cleanup_at") != job["cleanup_at"]:
            continue
        _safe_job_artifacts(current, data_dir)
        if store.clear_failed_expiry(job["id"], job["cleanup_at"]):
            store.event(job["id"], "job.failed_ttl_cleanup" if current["state"] == "FAILED" else "job.cancelled_ttl_cleanup", {"cleanup_at": job["cleanup_at"]})
            result["failed"] += 1
    return result


def sweep_orphan_spool(spool_dir: Path | None, store, *, now_ts: float | None = None) -> int:
    """Only old, untracked spool files, while there are no active P20 stages."""
    if spool_dir is None or not spool_dir.is_dir() or store.active_stage_count():
        return 0
    state_file = spool_dir / ".p20-flash-state.json"
    if not state_file.is_file():
        return 0  # fail closed, including damaged/unreadable state
    try:
        state = json.loads(state_file.read_text())
        paths = state["paths"]
        if not isinstance(paths, dict):
            return 0
        protected = {Path(v).name for v in paths.values()}
    except (ValueError, OSError, TypeError):
        return 0
    now_ts = now_ts if now_ts is not None else time.time()
    deleted = 0
    for path in spool_dir.iterdir():
        if path.is_symlink() or not path.is_file() or path.name.startswith(".") or path.name in protected:
            continue
        try:
            if now_ts - path.stat().st_mtime < RETENTION_SECONDS:
                continue
            if store.active_stage_count():
                break
            path.unlink()
            deleted += 1
        except (FileNotFoundError, PermissionError):
            continue
    return deleted
