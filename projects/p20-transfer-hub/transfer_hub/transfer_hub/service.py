from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Store
from .delivery import DeliveryError, QQFlashDelivery
from .runtime_agent import RuntimeAgent
from .aria2_rpc import Aria2RPC
from .aria2_daemon import Aria2Daemon
from .fetch_adapters import Aria2FetchAdapter, FetchAdapterRegistry, FetchCancelled, FetchPaused, FetchRequest
from .pipeline import PipelineScheduler, StageCancelled, StagePaused
from .domain import JobState, StageType, filename_from_url, random_password, safe_filename
from .engines import EngineError, pack_7z, pack_zip_aes, pack_zip_compat, verify_7z_password
from .resolver import ResolverError, ResolutionPlan, ResolvedItem, resolve_source
from .control import SkillRegistry, fingerprint
from .security import validate_public_http_url
from .archive_utils import extract_archive, is_supported_archive


def iso_after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _safe_source(source: str | None) -> str | None:
    if not source: return source
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(source)
    return urlunsplit((p.scheme, p.hostname or "", p.path, "[redacted]" if p.query else "", ""))


def _safe_error(error: str | None) -> str | None:
    if not error: return error
    import re
    from urllib.parse import urlsplit, urlunsplit
    value = re.sub(r"(?i)(cookie|authorization|token|password|secret)(\s*[:=]\s*)[^\s,;]+", r"\1\2[redacted]", error)
    value = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [redacted]", value)
    def clean_url(match):
        p = urlsplit(match.group(0).rstrip(").,;"))
        return urlunsplit((p.scheme, p.hostname or "", p.path, "[redacted]" if p.query else "", ""))
    value = re.sub(r"https?://[^\s\"'<>]+", clean_url, value, flags=re.I)
    return value[:500]


def _safe_event_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): ("[redacted]" if any(word in str(key).lower() for word in ("cookie", "authorization", "token", "password", "secret")) else _safe_event_data(item)) for key, item in value.items()}
    if isinstance(value, list): return [_safe_event_data(item) for item in value[-20:]]
    if isinstance(value, str): return _safe_error(value)
    return value


def _resolve_backend(value: str | None) -> str:
    """Map the "auto" delivery backend to the only wired backend (QQ Flash)."""
    return "qq_flash" if value in (None, "auto") else value


class TransferService:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.flash = QQFlashDelivery(settings.qqxfer_url, settings.qqxfer_token_file)
        self.runtime_agent = RuntimeAgent(settings.runtime_agent_url, settings.runtime_agent_token_file)
        aria2_secret = settings.aria2_secret_file
        if aria2_secret == Path("/var/lib/p20-transfer/aria2/rpc.secret") and settings.data_dir != Path("/var/lib/p20-transfer"):
            aria2_secret = settings.data_dir / "aria2" / "rpc.secret"
        self.aria2 = Aria2RPC(settings.aria2_url, aria2_secret)
        self.aria2_daemon = Aria2Daemon(settings.data_dir / "aria2", self.aria2, settings.fetch_concurrency)
        self.fetch_engines = FetchAdapterRegistry(Aria2FetchAdapter(self.aria2, self.aria2_daemon.start))
        self.skills = SkillRegistry(store)
        self._pipeline: PipelineScheduler | None = None
        self._maintenance_task: asyncio.Task | None = None
        self._last_progress: dict[str, float] = {}
        self._execution_locks_guard = threading.Lock()
        self._execution_locks: dict[str, threading.Lock] = {}
        self._history_lock = threading.RLock()
        self._browser_lock = threading.Lock()
        self._browser_users = 0

    def start(self) -> None:
        if self._pipeline is not None:
            return
        self._recover_after_restart()
        scheduler = PipelineScheduler(
            self.store,
            poll_seconds=self.settings.worker_poll_seconds,
            lease_seconds=self.settings.stage_lease_seconds,
            on_error=self._on_stage_error,
        )
        scheduler.add_pool(StageType.FETCH.value, self.settings.fetch_concurrency, self._stage_fetch)
        scheduler.add_pool(StageType.PROCESS.value, self.settings.process_concurrency, self._stage_process)
        scheduler.add_pool(StageType.DELIVER.value, self.settings.delivery_concurrency, self._stage_deliver)
        self._pipeline = scheduler
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    async def stop(self) -> None:
        if self._pipeline is not None:
            await self._pipeline.stop()
            self._pipeline = None
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except asyncio.CancelledError:
                pass
            self._maintenance_task = None
        self.aria2_daemon.stop()

    def create_url_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = payload["source"]
        filename = safe_filename(payload.get("filename") or filename_from_url(source))
        engine = payload.get("engine") or "auto"
        options = {
            "pack": bool(payload.get("pack", False)),
            "pack_format": payload.get("pack_format", "7z"),
            "encrypt": bool(payload.get("encrypt", False)),
            "volume_size": payload.get("volume_size"),
            "delivery_backend": _resolve_backend(payload.get("delivery_backend")),
            "delivery_profile": "unified",
            "ttl_seconds": 172800,
            "ack_policy": payload.get("ack_policy", "required"),
            "auth_ref": payload.get("auth_ref"),
            "engine_override": engine,
            "command_engine_hint": payload.get("command_engine_hint"),
            "resolver_override": payload.get("resolver") or "auto",
            "scheduled": bool(payload.get("scheduled", False)),
            "watcher_id": payload.get("watcher_id"),
            "watcher_name": payload.get("watcher_name"),
            "watch_run_id": payload.get("watch_run_id"),
            "watch_version": payload.get("watch_version"),
            "watch_notify": bool(payload.get("watch_notify", False)),
        }
        options["pack"] = True
        options["encrypt"] = True
        options["pack_format"] = "zip-aes256"
        password = payload.get("password") if options["encrypt"] else None
        if options["encrypt"] and not password:
            password = random_password()
        secret = {"archive_password": password} if password else {}
        job = self.store.create_job({
            "id": uuid.uuid4().hex,
            "source_type": payload.get("source_type", "url"),
            "source": source,
            "filename": filename,
            "title": payload.get("title") or filename,
            "engine": engine,
            "options": options,
            "secret": secret,
            "cleanup_at": None,
        })
        self._seed_pipeline(job)
        return self.store.get_job(job["id"])

    def create_local_job(self, upload_path: Path, filename: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        options = {
            "pack": bool(payload.get("pack", False)),
            "pack_format": payload.get("pack_format", "7z"),
            "encrypt": bool(payload.get("encrypt", False)),
            "volume_size": payload.get("volume_size"),
            "delivery_backend": _resolve_backend(payload.get("delivery_backend")),
            "delivery_profile": "unified",
            "ttl_seconds": 172800,
            "ack_policy": payload.get("ack_policy", "required"),
            "local_path": str(upload_path),
            "local_origin": payload.get("local_origin", "browser_upload"),
            "local_root": payload.get("local_root"),
            "local_display_path": payload.get("local_display_path"),
        }
        options["pack"] = True
        options["encrypt"] = True
        options["pack_format"] = "zip-aes256"
        password = (payload.get("password") or random_password()) if options["encrypt"] else None
        job = self.store.create_job({
            "id": uuid.uuid4().hex,
            "source_type": "local",
            "source": None,
            "filename": safe_filename(filename),
            "title": payload.get("title") or safe_filename(filename),
            "engine": "local",
            "options": options,
            "secret": {"archive_password": password} if password else {},
            "cleanup_at": None,
        })
        self.store.add_artifact(job["id"], StageType.FETCH.value, "source", str(upload_path),
                                size=upload_path.stat().st_size if upload_path.exists() else None,
                                sequence=0, metadata={"local": True})
        self._seed_pipeline(job)
        return self.store.get_job(job["id"])

    def _seed_pipeline(self, job: dict[str, Any]) -> None:
        if self.store.list_stages(job["id"]):
            return
        initial = StageType.PROCESS.value if job["source_type"] == "local" else StageType.FETCH.value
        self.store.ensure_stage(job["id"], initial)
        self.store.event(job["id"], "pipeline.seeded", {"stage": initial})

    def _fetch_stage_items(self, job_id: str) -> list[dict[str, Any]]:
        stage = self.store.latest_stage(job_id, StageType.FETCH.value)
        metadata = (stage or {}).get("metadata", {})
        items = metadata.get("items") or {}
        return [dict(value) for _, value in sorted(items.items(), key=lambda pair: int(pair[0]))]

    def pause(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        if job["state"] != JobState.FETCHING.value:
            raise ValueError("pause is only supported for active downloads")
        items = self._fetch_stage_items(job_id)
        if not items:
            raise ValueError("download engine has not started")
        supported = False
        for item in items:
            adapter = self.fetch_engines.get(str(item.get("engine") or ""))
            supported = adapter.pause(item) or supported
        if not supported:
            raise ValueError("pause is not supported by the active download engine")
        return self.store.transition(job_id, JobState.PAUSED, download_speed=0, eta_seconds=None)

    def resume(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        if job["state"] != JobState.PAUSED.value:
            raise ValueError("job is not paused")
        items = self._fetch_stage_items(job_id)
        supported = False
        for item in items:
            adapter = self.fetch_engines.get(str(item.get("engine") or ""))
            supported = adapter.resume(item) or supported
        if not supported:
            raise ValueError("resume is not supported by the active download engine")
        self.store.resume_stage(job_id, StageType.FETCH.value)
        return self.store.transition(job_id, JobState.FETCHING)

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        if job["state"] in {JobState.DONE.value, JobState.CANCELLED.value}:
            return job
        self.store.update(job_id, cancel_requested=1)
        for item in self._fetch_stage_items(job_id):
            try:
                self.fetch_engines.get(str(item.get("engine") or "")).cancel(item)
            except EngineError:
                pass
        self.store.cancel_active_stages(job_id)
        current = self.store.get_job(job_id)
        if current and current["state"] != JobState.READY.value:
            try:
                return self.store.transition(job_id, JobState.CANCELLED, download_speed=0, upload_speed=0, eta_seconds=None)
            except ValueError:
                return self.store.update(job_id, state=JobState.CANCELLED.value, completed_at=datetime.now(timezone.utc).isoformat())
        return current or job

    def _recover_after_restart(self) -> None:
        # Stage leases are the recovery mechanism for 0.5.0 jobs. A RUNNING
        # stage becomes claimable after lease expiry; completed artifacts remain.
        for job in self.store.jobs_in_states([JobState.QUEUED.value]):
            if not self.store.list_stages(job["id"]):
                self._seed_pipeline(job)

        interrupted = [
            JobState.FETCHING.value, JobState.VERIFYING.value, JobState.PACKING.value,
            JobState.ENCRYPTING.value, JobState.FLASH_UPLOADING.value, JobState.PAUSED.value,
        ]
        for job in self.store.jobs_in_states(interrupted):
            stages = self.store.list_stages(job["id"])
            if stages:
                self.store.event(job["id"], "job.recovered_after_restart", {
                    "previous_state": job["state"], "action": "stage_lease_recovery",
                })
                continue
            # Legacy 0.4.x active jobs did not persist stage/artifact boundaries.
            # Do not guess and risk duplicate delivery; deployment must drain them.
            self.store.event(job["id"], "job.recovered_after_restart", {
                "previous_state": job["state"], "action": "legacy_active_requires_retry",
            })
            self.store.update(
                job["id"], state=JobState.FAILED.value,
                error=f"legacy active state {job['state']} cannot be safely resumed after 0.5.0 cutover",
                download_speed=0, upload_speed=0, eta_seconds=None,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )

        for job in self.store.jobs_in_states([JobState.DOWNLOADED.value, JobState.CLEANUP.value]):
            self.store.event(job["id"], "job.recovered_after_restart", {
                "previous_state": job["state"], "action": "finish_cleanup",
            })
            try:
                self._cleanup(job)
            except Exception as exc:
                current = self.store.get_job(job["id"])
                if current and current["state"] == JobState.CLEANUP:
                    self.store.transition(job["id"], JobState.FAILED, error=f"restart cleanup failed: {type(exc).__name__}: {exc}")

    def retry(self, job_id: str) -> dict[str, Any]:
        with self._history_lock:
            job = self.store.get_job(job_id)
            if not job:
                raise KeyError(job_id)
            if job["state"] != JobState.FAILED.value:
                raise ValueError("job is not FAILED")
            stage = self.store.retry_failed_stage(job_id)
            if stage:
                state = {
                    StageType.FETCH.value: JobState.FETCHING.value,
                    StageType.PROCESS.value: JobState.VERIFYING.value,
                    StageType.DELIVER.value: JobState.VERIFYING.value,
                }.get(stage["stage"], JobState.QUEUED.value)
                self.store.update(job_id, state=state, retries=job["retries"] + 1, error=None,
                                  cancel_requested=0, completed_at=None, download_speed=0,
                                  upload_speed=0, eta_seconds=None)
                self.store.event(job_id, "pipeline.stage_retry", {"stage": stage["stage"], "attempt": stage["attempt"] + 1})
                return self.store.get_job(job_id)

            # Legacy failed job: start the new pipeline from the beginning.
            work = self.settings.data_dir / "jobs" / job_id
            shutil.rmtree(work, ignore_errors=True)
            self.store.reset_pipeline(job_id)
            updated = self.store.update(
                job_id, state=JobState.QUEUED.value, retries=job["retries"] + 1, error=None,
                cancel_requested=0, progress=0, upload_progress=0, download_speed=0,
                upload_speed=0, eta_seconds=None, original_size=None, final_size=None,
                sha256=None, artifact_path=None, result=None, completed_at=None,
            )
            if updated["source_type"] == "local":
                local_path = Path(updated["options"]["local_path"])
                self.store.add_artifact(job_id, StageType.FETCH.value, "source", str(local_path),
                                        size=local_path.stat().st_size if local_path.exists() else None,
                                        metadata={"local": True})
            self._seed_pipeline(updated)
            return self.store.get_job(job_id)

    def _remove_job_artifacts(self, job: dict[str, Any]) -> None:
        data_root = self.settings.data_dir.resolve()
        jobs_root = data_root / "jobs"
        work_candidate = jobs_root / job["id"]
        work_root = work_candidate.resolve()
        if work_root.is_relative_to(jobs_root.resolve()) and work_root != jobs_root.resolve():
            if work_candidate.is_symlink():
                work_candidate.unlink(missing_ok=True)
            else:
                shutil.rmtree(work_candidate, ignore_errors=True)
        local_path = job.get("options", {}).get("local_path")
        if local_path:
            path = Path(local_path).resolve()
            uploads_root = (data_root / "uploads").resolve()
            if path.is_relative_to(uploads_root):
                path.unlink(missing_ok=True)
        artifact_path = job.get("artifact_path")
        if artifact_path:
            path = Path(artifact_path).resolve()
            if path.is_relative_to(work_root):
                path.unlink(missing_ok=True)

    def clear_history_job(self, job_id: str) -> dict[str, Any]:
        with self._history_lock:
            job = self.store.get_job(job_id)
            if not job:
                raise KeyError(job_id)
            if job["state"] not in {JobState.DONE.value, JobState.FAILED.value, JobState.CANCELLED.value}:
                raise ValueError("only DONE, FAILED, or CANCELLED jobs can be cleared")
            self._remove_job_artifacts(job)
            if not self.store.delete_terminal_job(job_id):
                raise ValueError("job is no longer terminal")
            return {"job_id": job_id, "cleared": True}

    def clear_all_history(self) -> dict[str, Any]:
        with self._history_lock:
            jobs = self.store.terminal_jobs()
            cleared = []
            for job in jobs:
                self._remove_job_artifacts(job)
                if self.store.delete_terminal_job(job["id"]):
                    cleared.append(job["id"])
            return {"cleared": len(cleared), "job_ids": cleared}

    def clear_terminal_history(self) -> dict[str, int]:
        result = self.clear_all_history()
        return {"deleted": result["cleared"]}

    def retry_with_strategy(self, job_id: str, strategy: str) -> dict[str, Any]:
        strategies = {"default": {}, "direct": {"resolver_override": "direct", "engine_override": "aria2"},
                      "video": {"resolver_override": "auto", "engine_override": "yt-dlp"},
                      "manifest": {"resolver_override": "manifest", "engine_override": "nm3u8dl"}}
        if strategy not in strategies: raise ValueError("unsupported retry strategy")
        job = self.store.get_job(job_id)
        if not job: raise KeyError(job_id)
        options = dict(job["options"]); options.update(strategies[strategy])
        self.store.update(job_id, options=options)
        self.store.event(job_id, "job.retry_strategy", {"strategy": strategy})
        return self.retry(job_id)

    def ui_context(self, job_id: str | None = None) -> dict[str, Any]:
        if not job_id:
            jobs = self.store.list_jobs(20)
            return {"mode": "global", "jobs": [{"job_id": j["id"], "job_number": f"P20-{j['job_number']:06d}", "state": j["state"], "source": _safe_source(j.get("source")), "engine": j["engine"], "error": _safe_error(j.get("error"))} for j in jobs]}
        job = self.store.get_job(job_id)
        if not job: raise KeyError(job_id)
        events = self.store.recent_events(job_id, 12)
        opts = job["options"]
        return {"mode": "job", "job_id": job_id, "job_number": f"P20-{job['job_number']:06d}", "state": job["state"], "source": _safe_source(job.get("source")),
                "engine": job["engine"], "error": _safe_error(job.get("error")), "progress": job["progress"],
                "recent_events": [{"type": e["type"], "data": _safe_event_data(e["data"]), "created_at": e["created_at"]} for e in events],
                "share_link": ((job.get("result") or {}).get("items") or [{}])[0].get("share_link"),
                "matched_skill": opts.get("matched_skill_id") or opts.get("applied_skill_id"), "failure_fingerprint": opts.get("failure_fingerprint")}

    def revoke_share(self, job_id: str, confirmed: bool = False) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job: raise KeyError(job_id)
        plan = {"operation": "revoke_qq_flash_share", "job_id": job_id, "status": "pending", "capability": "browser"}
        self.store.event(job_id, "fallback.requested", {**plan, "confirmed": bool(confirmed)})
        import os
        endpoint = os.getenv("P20_BROWSER_RESOLVER_URL") or os.getenv("P20_BROWSER_AGENT_URL")
        if not confirmed:
            plan["status"] = "confirmation_required"
        elif not endpoint:
            plan["status"] = "unavailable"
        else:
            # The adapter accepts one fixed operation and never receives arbitrary navigation or code.
            try:
                import urllib.request
                req = urllib.request.Request(endpoint.rstrip("/") + "/operations/revoke_qq_flash_share",
                    data=__import__("json").dumps({"job_id": job_id, "share_link": ((job.get("result") or {}).get("items") or [{}])[0].get("share_link")}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=8) as response: payload = __import__("json").loads(response.read(65536))
                plan.update(status="completed" if payload.get("verified_invalid") is True else "pending", result=payload.get("status", "verification_pending"))
            except Exception as exc:
                plan.update(status="unavailable", detail=type(exc).__name__)
        self.store.event(job_id, "fallback.result", {k: v for k, v in plan.items() if k != "share_link"})
        return plan

    def _browser_acquire(self) -> None:
        with self._browser_lock:
            if self._browser_users == 0:
                result = self.runtime_agent.browser_start() or {}
                if self.runtime_agent.configured() and result.get("ok") is False:
                    raise RuntimeError(result.get("error") or "Browser Resolver failed to start")
            self._browser_users += 1

    def _browser_release(self) -> None:
        with self._browser_lock:
            self._browser_users = max(0, self._browser_users - 1)
            if self._browser_users == 0:
                self.runtime_agent.browser_stop()

    def _browser_resolve(self, job: dict[str, Any], original_error: Exception) -> ResolutionPlan:
        endpoint = os.getenv("P20_BROWSER_RESOLVER_URL") or os.getenv("P20_BROWSER_AGENT_URL")
        self.store.event(job["id"], "fallback.requested", {"operation": "resolve_source", "state": "pending", "trigger": type(original_error).__name__})
        if not endpoint:
            self.store.event(job["id"], "fallback.result", {"operation": "resolve_source", "state": "unavailable"})
            raise original_error
        self._browser_acquire()
        try:
            req = __import__("urllib.request").request.Request(endpoint.rstrip("/") + "/operations/resolve_source",
                data=json.dumps({"source": job["source"], "source_type": job["source_type"]}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with __import__("urllib.request").request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read(65536))
            target = str(data.get("url", ""))
            validate_public_http_url(target)
            engine = str(data.get("engine", "aria2"))
            if engine not in {"aria2", "yt-dlp", "gallery-dl", "nm3u8dl"}:
                raise ValueError("adapter returned unsupported engine")
            returned_headers = data.get("headers") or {}
            if not isinstance(returned_headers, dict):
                raise ValueError("adapter returned invalid headers")
            safe_headers = {str(key): str(value) for key, value in returned_headers.items()}
            self.store.event(job["id"], "fallback.result", {
                "operation": "resolve_source", "state": "completed", "engine": engine,
                "header_names": sorted(safe_headers),
            })
            return ResolutionPlan(
                "browser-fallback",
                [ResolvedItem(target, engine, job["filename"], "stream" if engine == "nm3u8dl" else "file", safe_headers)],
                {"fallback": "browser", "header_names": sorted(safe_headers)},
            )
        except Exception as exc:
            self.store.event(job["id"], "fallback.result", {"operation": "resolve_source", "state": "unavailable", "error_class": type(exc).__name__})
            raise original_error
        finally:
            self._browser_release()

    def ack(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        if job["state"] != JobState.READY:
            raise ValueError("job is not READY")
        job = self.store.transition(job_id, JobState.RETRIEVED)
        return self._cleanup(job)

    def _resolve_headers(self, auth_ref: str | None) -> dict[str, str]:
        if not auth_ref:
            return {}
        if not auth_ref.replace("-", "").replace("_", "").isalnum():
            raise ValueError("invalid auth_ref")
        path = self.settings.secrets_dir / f"{auth_ref}.json"
        data = json.loads(path.read_text())
        headers = data.get("headers") or {}
        if not isinstance(headers, dict):
            raise ValueError("secret headers must be an object")
        return {str(k): str(v) for k, v in headers.items()}

    def _on_download_message(self, job_id: str, text: str) -> None:
        self.store.event(job_id, "job.download_message", {"message": _safe_error(text)})
        self.store.update(job_id, updated_at=datetime.now(timezone.utc).isoformat())  # Refresh the selected task's event timeline.

    def _on_progress(self, job_id: str, pct: float | None, speed: int | None, eta: int | None) -> None:
        now = time.monotonic()
        last = self._last_progress.get(job_id, 0.0)
        if pct is None and now - last < 1.0:
            return
        if now - last < 0.8 and pct not in {0, 100}:
            return
        self._last_progress[job_id] = now
        current = self.store.get_job(job_id)
        value = pct if pct is not None else (current["progress"] if current else 0)
        self.store.update(job_id, progress=value, download_speed=speed or 0, eta_seconds=eta)
        self.store.event(job_id, "job.progress", {"progress": value, "download_speed": speed or 0, "eta_seconds": eta})

    def _on_upload_progress(self, job_id: str, pct: float, speed: int, eta: int | None) -> None:
        now = time.monotonic()
        key = f"upload:{job_id}"
        last = self._last_progress.get(key, 0.0)
        if now - last < 0.4 and pct not in {0, 100}:
            return
        self._last_progress[key] = now
        self.store.update(
            job_id,
            upload_progress=max(0.0, min(100.0, pct)),
            upload_speed=max(0, speed),
            eta_seconds=eta,
        )
        self.store.event(job_id, "job.upload_progress", {
            "upload_progress": max(0.0, min(100.0, pct)),
            "upload_speed": max(0, speed),
            "eta_seconds": eta,
        })

    def _ensure_runtime(self, job_id: str, timeout: float = 200.0) -> None:
        """Bring the QQ runtime up on demand before a Flash delivery.

        The runtime is normally stopped to save ~420 MB on a 2 vCPU host.
        A transfer that needs QQ Flash starts it automatically so unattended
        callers (MCP) and the UI both just work. Never removes containers.
        """
        status = (self.runtime_agent.status() or {}).get("status", {}) or {}
        state = status.get("state")
        if state == "running":
            return
        if state is None:
            # Agent not reachable/configured (tests, single-process dev): the
            # QQ endpoint is assumed reachable and delivery reports any failure.
            return
        # Known stopped/starting -> bring it up on demand.
        self.store.event(job_id, "job.runtime", {"action": "start", "state": state})
        self.store.update(job_id, updated_at=datetime.now(timezone.utc).isoformat())
        result = self.runtime_agent.start() or {}
        new = result.get("status") or {}
        self.store.event(job_id, "job.runtime",
                         {"action": "started", "state": new.get("state"),
                          "logged_in": new.get("logged_in")})
        if new.get("state") == "running":
            return
        if result.get("error"):
            return  # unreachable agent: let delivery surface the real error
        raise DeliveryError(
            "QQ runtime did not start; open the P20 page and press 启用 QQ Runtime, "
            "or start it manually, then retry the transfer."
        )

    def _cancelled(self, job_id: str) -> bool:
        job = self.store.get_job(job_id)
        return bool(job and job["cancel_requested"])

    def _hash(self, path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def _hash_artifacts(self, paths: list[Path]) -> str:
        if len(paths) == 1:
            return self._hash(paths[0])
        h = hashlib.sha256()
        for path in sorted(paths, key=lambda p: p.name):
            h.update(path.name.encode("utf-8", errors="replace"))
            h.update(b"\0")
            h.update(self._hash(path).encode())
            h.update(b"\n")
        return h.hexdigest()

    def _cleanup(self, job: dict[str, Any]) -> dict[str, Any]:
        self.store.transition(job["id"], JobState.CLEANUP)
        root = self.settings.data_dir / "jobs" / job["id"]
        shutil.rmtree(root, ignore_errors=True)
        local_path = job["options"].get("local_path")
        if local_path and job["options"].get("local_origin") != "vps":
            Path(local_path).unlink(missing_ok=True)
        return self.store.transition(job["id"], JobState.DONE, progress=100)

    def _cleanup_cancelled(self, job_id: str, work: Path) -> None:
        shutil.rmtree(work, ignore_errors=True)
        current = self.store.get_job(job_id)
        if not current:
            return
        local_path = current["options"].get("local_path")
        if local_path and current["options"].get("local_origin") != "vps":
            Path(local_path).unlink(missing_ok=True)

    def _on_stage_error(self, stage: str, claim: dict[str, Any], exc: BaseException) -> None:
        jid = claim["job_id"]
        job = self.store.get_job(jid)
        if not job or job["state"] in {JobState.CANCELLED.value, JobState.DONE.value}:
            return
        error = f"{type(exc).__name__}: {_safe_error(str(exc)) or str(exc)}"
        fp = self.store.record_fingerprint(fingerprint(job, stage.lower(), exc if isinstance(exc, Exception) else error))
        options = {**job.get("options", {}), "failure_fingerprint": fp["fingerprint"]}
        self.store.update(
            jid,
            state=JobState.FAILED.value,
            error=error,
            options=options,
            download_speed=0,
            upload_speed=0,
            eta_seconds=None,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        self.store.event(jid, "failure.fingerprinted", {"fingerprint": fp["fingerprint"], "error_class": fp["error_class"]})
        self.store.event(jid, "pipeline.stage_failed", {"stage": stage, "error_class": type(exc).__name__})

    def _resolve_job(self, job: dict[str, Any]) -> tuple[ResolutionPlan, str]:
        headers = self._resolve_headers(job["options"].get("auth_ref"))
        applied = job["options"].get("applied_skill_id")
        skill = self.skills.matching(job["source"], job["source_type"])
        if applied:
            skill = self.store.get_skill(applied)
        elif skill:
            job = self.skills.apply(skill["id"], job)
            job = self.store.get_job(job["id"])
        if skill and skill["state"] != "degraded" and skill["config"].get("auth_ref"):
            headers = self._resolve_headers(skill["config"]["auth_ref"])
        engine_override = job["options"].get("engine_override") or (
            job["engine"] if job["engine"] in {"aria2", "yt-dlp", "nm3u8dl", "gallery-dl"} else "auto"
        )
        try:
            plan = resolve_source(
                job["source_type"], job["source"], job["filename"], headers, engine_override,
                job["options"].get("resolver_override") or "auto",
            )
        except ResolverError as exc:
            try:
                plan = self._browser_resolve(job, exc)
            except ResolverError:
                self.store.event(job["id"], "resolver.exhausted", {
                    "error_class": type(exc).__name__,
                    "source_type": job["source_type"],
                    "engine_override": engine_override,
                })
                raise
        if (
            engine_override == "auto"
            and (job["options"].get("resolver_override") or "auto") == "auto"
            and job["source_type"] in {"auto", "url"}
            and plan.resolver == "direct"
            and plan.engine_label == "aria2"
            and (os.getenv("P20_BROWSER_RESOLVER_URL") or os.getenv("P20_BROWSER_AGENT_URL"))
        ):
            try:
                discovered = self._browser_resolve(job, ResolverError("generic URL browser discovery"))
                if discovered.engine_label == "nm3u8dl" or discovered.items[0].source != job["source"]:
                    plan = discovered
            except ResolverError:
                pass
        self.store.update(job["id"], engine=plan.engine_label)
        self.store.event(job["id"], "job.resolved", {
            "resolver": plan.resolver,
            "engine": plan.engine_label,
            "items": [{"engine": item.engine, "kind": item.kind} for item in plan.items],
            "media_count": plan.metadata.get("media_count"),
            "header_names": plan.metadata.get("header_names") or sorted(headers),
        })
        return plan, plan.resolver

    def _fetch_item_metadata(self, claim: dict[str, Any], index: int, engine: str) -> dict[str, Any]:
        stage = self.store.latest_stage(claim["job_id"], StageType.FETCH.value) or claim
        metadata = dict(stage.get("metadata") or {})
        items = dict(metadata.get("items") or {})
        item = dict(items.get(str(index)) or {})
        if item.get("engine") != engine:
            item = {"engine": engine}
            items[str(index)] = item
            self.store.update_stage_metadata(int(claim["id"]), str(claim["claim_token"]), items=items)
        return item

    def _update_fetch_item_metadata(self, claim: dict[str, Any], index: int, patch: dict[str, Any]) -> None:
        stage = self.store.latest_stage(claim["job_id"], StageType.FETCH.value) or claim
        metadata = dict(stage.get("metadata") or {})
        items = dict(metadata.get("items") or {})
        item = dict(items.get(str(index)) or {})
        item.update(patch)
        items[str(index)] = item
        self.store.update_stage_metadata(int(claim["id"]), str(claim["claim_token"]), items=items)

    def _fetch_with_adapter(self, claim: dict[str, Any], item: ResolvedItem, item_dir: Path, index: int,
                            total_items: int) -> list[Path]:
        jid = claim["job_id"]
        metadata = self._fetch_item_metadata(claim, index, item.engine)
        adapter = self.fetch_engines.get(item.engine)
        request = FetchRequest(
            source=item.source, output_dir=item_dir, filename=item.filename, headers=item.headers,
            engine_options=item.engine_options, cancelled=lambda: self._cancelled(jid),
            progress=lambda pct, speed, eta: self._on_progress(
                jid, ((index + (pct or 0) / 100.0) / total_items * 100.0) if pct is not None else None,
                speed, eta,
            ),
            message=lambda text: self._on_download_message(jid, text),
        )
        try:
            return adapter.fetch(request, metadata, lambda patch: self._update_fetch_item_metadata(claim, index, patch))
        except FetchPaused as exc:
            raise StagePaused() from exc
        except FetchCancelled as exc:
            raise StageCancelled() from exc

    def _stage_fetch(self, claim: dict[str, Any]) -> None:
        jid = claim["job_id"]
        job = self.store.get_job(jid)
        if not job or self._cancelled(jid):
            raise StageCancelled()
        if job["state"] == JobState.QUEUED.value:
            job = self.store.transition(jid, JobState.FETCHING)
        elif job["state"] != JobState.FETCHING.value:
            job = self.store.update(jid, state=JobState.FETCHING.value, completed_at=None)
        work = self.settings.data_dir / "jobs" / jid
        work.mkdir(parents=True, exist_ok=True)
        plan, resolver_used = self._resolve_job(job)
        total_items = max(1, len(plan.items))
        existing = {a["sequence"]: a for a in self.store.list_artifacts(jid, StageType.FETCH.value)}
        for index, item in enumerate(plan.items):
            old = existing.get(index)
            if old and Path(old["path"]).is_file():
                continue
            item_dir = work / f"source-{index + 1}"
            used_engine = item.engine
            try:
                paths = self._fetch_with_adapter(claim, item, item_dir, index, total_items)
            except EngineError as exc:
                message = str(exc).lower()
                protected = any(marker in message for marker in ("403", "forbidden", "401", "unauthorized", "cloudflare", "challenge"))
                if not protected or total_items != 1 or resolver_used == "browser-fallback":
                    raise
                self.store.event(jid, "download.protected_source", {
                    "engine": item.engine, "resolver": resolver_used, "action": "browser_fallback",
                })
                fallback = self._browser_resolve(job, exc)
                fallback_item = fallback.items[0]
                used_engine = fallback_item.engine
                self.store.update(jid, engine=fallback.engine_label)
                shutil.rmtree(item_dir, ignore_errors=True)
                self._update_fetch_item_metadata(claim, index, {"engine": used_engine, "engine_task_id": None})
                paths = self._fetch_with_adapter(claim, fallback_item, item_dir, index, total_items)
            for sub_index, path in enumerate(paths):
                sequence = index * 1000 + sub_index
                self.store.add_artifact(jid, StageType.FETCH.value, "source", str(path),
                                        size=path.stat().st_size, sequence=sequence,
                                        metadata={"engine": used_engine, "resolver": resolver_used})
        if self._cancelled(jid):
            raise StageCancelled()
        self.store.transition(jid, JobState.VERIFYING, download_speed=0, eta_seconds=None)
        self.store.ensure_stage(jid, StageType.PROCESS.value, sequence=1)

    def _validated_local_source(self, job: dict[str, Any], fallback: Path) -> Path:
        if job["options"].get("local_origin") != "vps":
            return fallback
        roots = self.settings.local_file_roots()
        display_path = job["options"].get("local_display_path") or ""
        try:
            rel = Path(display_path) if display_path else None
            alias = rel.parts[0] if rel and rel.parts else ""
            if alias in roots:
                local_root = roots[alias].resolve(strict=True)
                path = local_root.joinpath(*rel.parts[1:]).resolve(strict=True)
            else:
                path = fallback.resolve(strict=True)
                local_root = next((r.resolve() for r in roots.values() if r.exists() and path.is_relative_to(r.resolve())), None)
                if local_root is None:
                    raise ValueError("selected VPS file is outside the current file roots")
        except FileNotFoundError as exc:
            raise ValueError("selected VPS local file no longer exists") from exc
        if not path.is_file() or not path.is_relative_to(local_root):
            raise ValueError("selected VPS local file is not available")
        return path

    def _public_names(self, jid: str, parts: list[Path]) -> list[str]:
        neutral_names = ("runtime-components", "sdk-resources", "container-base", "system-tools", "offline-resources")
        public_stem = neutral_names[int(jid[:8], 16) % len(neutral_names)]
        public_version = f"{int(jid[8:10],16)%9+1}.{int(jid[10:12],16)%10}.{int(jid[12:14],16)%10}"
        public_prefix = f"{public_stem}-v{public_version}-x64"
        names: list[str] = []
        for index, part in enumerate(parts, 1):
            if part.name.startswith(public_prefix):
                names.append(part.name)
                continue
            suffix = part.suffix or ".bin"
            name = public_prefix
            if len(parts) > 1:
                name += f"-part{index:02d}"
            names.append(f"{name}{suffix}")
        return names

    def _stage_process(self, claim: dict[str, Any]) -> None:
        jid = claim["job_id"]
        job = self.store.get_job(jid)
        if not job or self._cancelled(jid):
            raise StageCancelled()
        if job["state"] == JobState.QUEUED.value:
            job = self.store.transition(jid, JobState.VERIFYING)
        source_rows = self.store.list_artifacts(jid, StageType.FETCH.value)
        if not source_rows:
            raise EngineError("PROCESS has no FETCH artifacts")
        artifacts = [Path(row["path"]) for row in source_rows]
        if job["source_type"] == "local":
            artifacts[0] = self._validated_local_source(job, artifacts[0])
        if not all(path.is_file() for path in artifacts):
            raise EngineError("one or more FETCH artifacts are missing")
        work = self.settings.data_dir / "jobs" / jid
        work.mkdir(parents=True, exist_ok=True)
        for old in self.store.delete_artifacts(jid, StageType.PROCESS.value):
            path = Path(old["path"])
            try:
                if path.resolve().is_relative_to(work.resolve()) and path.is_file():
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        if job["options"].get("delivery_profile") in {"iphone", "android"}:
            extracted: list[Path] = []
            extracted_archive = False
            for index, artifact in enumerate(artifacts):
                # APK is a ZIP container by format, but it is an installable Android
                # package and must be delivered intact to mobile clients.
                if artifact.suffix.lower() != ".apk" and is_supported_archive(artifact):
                    extracted_archive = True
                    destination = work / f"mobile-extracted-{index + 1}"
                    shutil.rmtree(destination, ignore_errors=True)
                    extracted.extend(extract_archive(artifact, destination))
                else:
                    extracted.append(artifact)
            artifacts = extracted
            self.store.event(jid, "job.mobile_ready", {
                "profile": job["options"]["delivery_profile"],
                "encrypted": False,
                "extracted_archive": extracted_archive,
            })
        sha = self._hash_artifacts(artifacts)
        original_size = sum(path.stat().st_size for path in artifacts)
        self.store.update(jid, sha256=sha, original_size=original_size)
        options = self.store.get_job(jid)["options"]
        if options.get("delivery_profile") not in {"iphone", "android"} and (options.get("pack") or options.get("encrypt")):
            target_state = JobState.ENCRYPTING if options.get("encrypt") else JobState.PACKING
            current = self.store.get_job(jid)
            if current["state"] != target_state.value:
                self.store.transition(jid, target_state)
            pack_format = options.get("pack_format", "zip-aes256")
            suffix = ".zip" if pack_format in {"zip", "zip-aes256"} else ".7z"
            neutral_names = ("runtime-components", "sdk-resources", "container-base", "system-tools", "offline-resources")
            stem = neutral_names[int(jid[:8], 16) % len(neutral_names)]
            version = f"{int(jid[8:10],16)%9+1}.{int(jid[10:12],16)%10}.{int(jid[12:14],16)%10}"
            archive = work / f"{stem}-v{version}-x64{suffix}"
            archive.unlink(missing_ok=True)
            pack_source = artifacts[0]
            if len(artifacts) > 1:
                pack_source = work / "bundle"
                shutil.rmtree(pack_source, ignore_errors=True)
                pack_source.mkdir(parents=True, exist_ok=True)
                for index, source_path in enumerate(artifacts, 1):
                    target = pack_source / safe_filename(source_path.name or f"media-{index}")
                    if target.exists():
                        target = pack_source / f"{index:02d}-{target.name}"
                    shutil.copy2(source_path, target)
            password = job["secret"].get("archive_password")
            packer = pack_zip_aes if pack_format == "zip-aes256" else (pack_zip_compat if pack_format == "zip" else pack_7z)
            parts = packer(pack_source, archive, password, options.get("volume_size"), lambda: self._cancelled(jid))
            if password:
                verify_7z_password(parts[0], password, lambda: self._cancelled(jid))
        else:
            parts = artifacts
        if self._cancelled(jid):
            raise StageCancelled()
        public_names = self._public_names(jid, parts)
        processed_parts: list[Path] = []
        if options.get("delivery_backend") in {"none", None}:
            for part, public_name in zip(parts, public_names):
                target = work / public_name
                if part.name == public_name:
                    target = part
                elif part != target:
                    shutil.copy2(part, target)
                processed_parts.append(target)
        else:
            processed_parts = parts
        for index, (part, public_name) in enumerate(zip(processed_parts, public_names)):
            self.store.add_artifact(
                jid, StageType.PROCESS.value, "delivery", str(part), size=part.stat().st_size,
                sha256=self._hash(part), sequence=index, metadata={"public_name": public_name},
            )
        options = {**options, "public_names": public_names}
        total_final = sum(path.stat().st_size for path in processed_parts)
        self.store.update(jid, options=options, final_size=total_final, artifact_path=str(processed_parts[0]))
        self.store.ensure_stage(jid, StageType.DELIVER.value, sequence=2)

    def _deliver_qq_artifact(self, job: dict[str, Any], artifact: dict[str, Any], index: int,
                             uploaded_before: int, total_final: int) -> dict[str, Any]:
        jid = job["id"]
        path = Path(artifact["path"])
        public_name = str(artifact.get("metadata", {}).get("public_name") or path.name)
        part_size = path.stat().st_size
        key = f"{jid}:{artifact['id']}:{artifact.get('sha256') or part_size}"

        def on_upload(pct: float, speed: int, eta: int | None) -> None:
            uploaded = uploaded_before + (part_size * pct / 100.0)
            overall_pct = (uploaded / total_final * 100.0) if total_final else 100.0
            overall_eta = int(max(0, total_final - uploaded) / speed) if speed > 0 else eta
            self._on_upload_progress(jid, overall_pct, speed, overall_eta)

        attempt = self.store.get_delivery_attempt(jid, key)
        if attempt and attempt["state"] == "DONE" and attempt.get("result"):
            result = dict(attempt["result"])
            result["filename"] = public_name
            return result
        if attempt and attempt.get("provider_task_id"):
            try:
                result = self.flash.wait_existing(
                    str(attempt["provider_task_id"]), progress=on_upload,
                    cancel=lambda: self._cancelled(jid),
                )
                result["filename"] = public_name
                self.store.upsert_delivery_attempt(
                    jid, "qq_flash", key, artifact_id=artifact["id"],
                    provider_task_id=str(attempt["provider_task_id"]), state="DONE", result=result,
                )
                return result
            except DeliveryError as exc:
                if "unknown file_set_id" not in str(exc):
                    raise
        self.store.upsert_delivery_attempt(jid, "qq_flash", key, artifact_id=artifact["id"], state="STARTING")

        def started(file_set_id: str) -> None:
            self.store.upsert_delivery_attempt(
                jid, "qq_flash", key, artifact_id=artifact["id"],
                provider_task_id=file_set_id, state="RUNNING",
            )

        try:
            result = self.flash.upload(
                path, public_name, filename=public_name, progress=on_upload, started=started,
                idempotency_key=key, cancel=lambda: self._cancelled(jid),
            )
        except Exception as exc:
            latest = self.store.get_delivery_attempt(jid, key)
            self.store.upsert_delivery_attempt(
                jid, "qq_flash", key, artifact_id=artifact["id"],
                provider_task_id=(latest or {}).get("provider_task_id"), state="FAILED", error=str(exc)[:1000],
            )
            raise
        result["filename"] = public_name
        self.store.upsert_delivery_attempt(
            jid, "qq_flash", key, artifact_id=artifact["id"],
            provider_task_id=str(result.get("file_set_id") or "") or None, state="DONE", result=result,
        )
        return result

    def _stage_deliver(self, claim: dict[str, Any]) -> None:
        jid = claim["job_id"]
        job = self.store.get_job(jid)
        if not job or self._cancelled(jid):
            raise StageCancelled()
        artifacts = self.store.list_artifacts(jid, StageType.PROCESS.value)
        if not artifacts:
            raise DeliveryError("DELIVER has no PROCESS artifacts")
        if not all(Path(item["path"]).is_file() for item in artifacts):
            raise DeliveryError("one or more PROCESS artifacts are missing")
        options = job["options"]
        backend = options.get("delivery_backend")
        if backend == "qq_flash":
            self._ensure_runtime(jid)
            current = self.store.get_job(jid)
            if current["state"] != JobState.FLASH_UPLOADING.value:
                self.store.transition(jid, JobState.FLASH_UPLOADING, upload_progress=0, upload_speed=0, eta_seconds=None)
            total_final = sum(int(item.get("size") or Path(item["path"]).stat().st_size) for item in artifacts)
            results: list[dict[str, Any]] = []
            uploaded_before = 0
            for index, artifact in enumerate(artifacts):
                if self._cancelled(jid):
                    raise StageCancelled()
                result = self._deliver_qq_artifact(job, artifact, index, uploaded_before, total_final)
                results.append(result)
                uploaded_before += int(artifact.get("size") or Path(artifact["path"]).stat().st_size)
            result_payload = {"backend": "qq_flash", "items": results}
            ready_fields = {"result": result_payload, "progress": 100, "upload_progress": 100,
                            "upload_speed": 0, "eta_seconds": 0}
        elif backend in {"none", None}:
            result_payload = {"backend": "none", "files": [Path(item["path"]).name for item in artifacts]}
            ready_fields = {"result": result_payload, "progress": 100}
        else:
            raise DeliveryError(f"unsupported delivery backend: {backend}")
        current = self.store.get_job(jid)
        if current["state"] != JobState.READY.value:
            self.store.transition(jid, JobState.READY, **ready_fields)
        if options.get("matched_skill_id"):
            self.skills.validate(options["matched_skill_id"], True, "transfer completed")
        if options.get("ack_policy") == "none":
            self._cleanup(self.store.get_job(jid))

    def _execute(self, job: dict[str, Any]) -> None:
        """Synchronous compatibility runner used by tests and one-shot callers."""
        jid = job["id"]
        self._seed_pipeline(self.store.get_job(jid))
        handlers = {
            StageType.FETCH.value: self._stage_fetch,
            StageType.PROCESS.value: self._stage_process,
            StageType.DELIVER.value: self._stage_deliver,
        }
        for _ in range(8):
            current = self.store.get_job(jid)
            if not current or current["state"] in {JobState.READY.value, JobState.DONE.value, JobState.FAILED.value, JobState.CANCELLED.value}:
                return
            progressed = False
            for stage_name in (StageType.FETCH.value, StageType.PROCESS.value, StageType.DELIVER.value):
                claim = self.store.claim_stage_for_job(jid, stage_name, "inline", self.settings.stage_lease_seconds)
                if not claim:
                    continue
                progressed = True
                try:
                    handlers[stage_name](claim)
                except StagePaused:
                    self.store.pause_stage(claim["id"], claim["claim_token"])
                    return
                except StageCancelled:
                    self.store.cancel_active_stages(jid)
                    return
                except Exception as exc:
                    self.store.finish_stage(claim["id"], claim["claim_token"], success=False, error=str(exc)[:1000])
                    self._on_stage_error(stage_name, claim, exc)
                    return
                else:
                    self.store.finish_stage(claim["id"], claim["claim_token"], success=True)
                break
            if not progressed:
                return

    def _cleanup_expired_ready(self) -> None:
        # Compatibility for pre-0.5.15 integration callers and regression tests.
        self._cleanup_expired_jobs()

    def _cleanup_expired_failed(self) -> None:
        # Both legacy entry points use the same state-safe policy.
        self._cleanup_expired_jobs()

    def _cleanup_expired_jobs(self) -> None:
        from .retention import sweep_jobs
        sweep_jobs(self.store, self.settings.data_dir)

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            await asyncio.to_thread(self._cleanup_expired_jobs)
