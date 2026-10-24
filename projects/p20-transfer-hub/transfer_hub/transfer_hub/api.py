from __future__ import annotations

from transfer_hub import __version__
import asyncio
import json
import ipaddress
import os
import secrets
import shlex
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl

from .config import Settings
from .db import Store
from .delivery import DeliveryError
from .domain import JobState, safe_filename
from .engines import available_tools
from .security import validate_public_http_url
from .runtime_agent import RuntimeAgent
from .service import TransferService
from .control import fingerprint
from .resolver import resolve_source as analyze_source
from .receiver_ticket import build_receiver_ticket
from .watcher_notify import WatcherNotifyError, WatcherWebhook, build_test_payload
from .cmcloud import CMCloudBroker, CMCloudError, CMCloudProvider


class CreateJob(BaseModel):
    source_type: Literal["auto", "url", "github", "stream", "m3u8", "hls", "dash", "ism", "video", "web_video", "playlist"] = "auto"
    source: str = Field(min_length=1, max_length=12000)
    filename: str | None = None
    title: str | None = None
    resolver: Literal["auto", "direct", "twitter", "manifest", "webpage-manifest"] = "auto"
    engine: Literal["auto", "aria2", "yt-dlp", "gallery-dl", "nm3u8dl", "uav"] = "auto"
    volume_size: str | None = None
    delivery_backend: Literal["auto", "qq_flash", "none"] = "auto"
    delivery_profile: Literal["unified", "desktop", "iphone", "android"] = "unified"
    ttl_seconds: int = Field(default=172800, ge=300, le=604800)
    ack_policy: Literal["required", "none"] = "required"
    auth_ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,80}$")


class CreateLocalFileJob(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    title: str | None = None
    volume_size: str | None = None
    delivery_backend: Literal["qq_flash", "none"] = "qq_flash"
    delivery_profile: Literal["unified", "desktop", "iphone", "android"] = "unified"
    ttl_seconds: int = Field(default=172800, ge=300, le=604800)
    ack_policy: Literal["required", "none"] = "required"


class CMCloudCreateUpload(BaseModel):
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")
    name: str | None = Field(default=None, max_length=255)
    parent_id: str = Field(default="/", min_length=1, max_length=200)
    encryption: Literal["none", "e2ee"] = "none"
    opaque_metadata: str | None = Field(default=None, max_length=16384)
    part_size: int | None = Field(default=None, ge=1024 * 1024, le=1024 * 1024 * 1024)


class CMCloudIssueParts(BaseModel):
    part_numbers: list[int] = Field(min_length=1, max_length=100)


def _source_from_input(value: str) -> tuple[str, str | None]:
    raw = value.strip()
    from .uav import media_code
    code = media_code(raw)
    if code:
        return code, None
    if raw.startswith(("http://", "https://")) and not any(ch.isspace() for ch in raw):
        return raw, None
    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        raise ValueError(f"invalid command: {exc}") from exc
    urls = [token for token in tokens if token.startswith(("http://", "https://"))]
    if not urls:
        raise ValueError("input must contain an HTTP/HTTPS URL")
    command = Path(tokens[0]).name.lower() if tokens else ""
    engine = None
    if "n_m3u8dl-re" in command or "nm3u8dl" in command:
        engine = "nm3u8dl"
    elif "yt-dlp" in command or "youtube-dl" in command:
        engine = "yt-dlp"
    elif "aria2c" in command or command == "aria2":
        engine = "aria2"
    return urls[0], engine


class RetryStrategy(BaseModel):
    strategy: Literal["default", "direct", "video", "manifest"] = "default"


class PromoteSkill(BaseModel):
    state: Literal["validated", "stable"] = "validated"


class ApplySkill(BaseModel):
    skill_id: str = Field(min_length=1, max_length=80)


class RevokeShareRequest(BaseModel):
    confirm_irreversible: bool = False


class WatcherEnqueue(BaseModel):
    watch_run_id: int = Field(gt=0)
    source: str = Field(min_length=1, max_length=12000)
    filename: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    version: str = Field(min_length=1, max_length=200)
    details: dict = Field(default_factory=dict)


def create_app(settings: Settings | None = None, start_worker: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    store = Store(settings.data_dir / "transfer.db")
    service = TransferService(settings, store)
    runtime_agent = RuntimeAgent(settings.runtime_agent_url, settings.runtime_agent_token_file)
    cmcloud_provider = CMCloudProvider(
        settings.cmcloud_authorization_file or (settings.secrets_dir / "cmcloud.authorization"),
        settings.cmcloud_state_file or (settings.data_dir / "cmcloud" / "state.json"),
    )
    cmcloud = CMCloudBroker(cmcloud_provider, settings.data_dir)
    local_file_roots = settings.local_file_roots()

    def resolve_local_file(virtual_path: str, *, require_file: bool = False) -> tuple[str, Path, Path]:
        rel = Path(virtual_path)
        if rel.is_absolute() or "\x00" in virtual_path or not rel.parts:
            raise ValueError("local file path must be inside an allowed VPS file area")
        alias = rel.parts[0]
        root = local_file_roots.get(alias)
        if root is None:
            raise ValueError("local file root is not allowed")
        try:
            candidate = (root.joinpath(*rel.parts[1:])).resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError("local file does not exist") from exc
        if not root.exists() or candidate != root and not candidate.is_relative_to(root):
            raise ValueError("local file path escapes the VPS file area")
        if require_file and not candidate.is_file():
            raise ValueError("selected path is not a file")
        return alias, root, candidate

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_worker:
            service.start()
        try:
            yield
        finally:
            if start_worker:
                await service.stop()

    app = FastAPI(title="P20 Tokyo Transfer Hub", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.service = service
    app.state.runtime_agent = runtime_agent
    app.state.skills = service.skills
    app.state.cmcloud = cmcloud
    app.state.cmcloud_provider = cmcloud_provider
    app.state.last_request_touch = 0.0
    request_marker = settings.data_dir / "hub-last-request"

    @app.middleware("http")
    async def touch_hub_activity(request: Request, call_next):
        internal_probe = (
            request.url.path == "/health"
            and request.headers.get("X-P20-Healthcheck") == "1"
        )
        if not internal_probe:
            now = time.monotonic()
            if now - app.state.last_request_touch >= 30:
                try:
                    request_marker.touch(exist_ok=True)
                    app.state.last_request_touch = now
                except OSError:
                    pass
        return await call_next(request)

    def api_token_matches(authorization: str | None) -> bool:
        if not settings.api_token_file.exists():
            return False
        expected = settings.api_token_file.read_text().strip()
        supplied = ""
        if authorization and authorization.startswith("Bearer "):
            supplied = authorization[7:]
        return bool(expected and secrets.compare_digest(supplied, expected))

    def trusted_private_client(request: Request) -> bool:
        host = request.client.host if request.client else ""
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return False
        if addr.is_loopback:
            return True
        if addr.version == 4 and addr in ipaddress.ip_network("100.64.0.0/10"):
            return True
        if addr.version == 6 and addr in ipaddress.ip_network("fd7a:115c:a1e0::/48"):
            return True
        return False

    def require_auth(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> None:
        if trusted_private_client(request) or api_token_matches(authorization):
            return
        raise HTTPException(401, "unauthorized")

    def present(job: dict, reveal_secret: bool = False) -> dict:
        out = dict(job)
        number = out.get("job_number")
        out["job_number_label"] = f"P20-{number:06d}" if number is not None else None
        options_for_name = dict(out.get("options") or {})
        public_names = list(options_for_name.get("public_names") or [])
        if public_names:
            out["public_name_preview"] = public_names[0]
        else:
            jid = str(out.get("id") or "0" * 32)
            neutral_names = ("runtime-components", "sdk-resources", "container-base", "system-tools", "offline-resources")
            try:
                stem = neutral_names[int(jid[:8], 16) % len(neutral_names)]
                version = f"{int(jid[8:10],16)%9+1}.{int(jid[10:12],16)%10}.{int(jid[12:14],16)%10}"
            except ValueError:
                stem, version = "runtime-components", "1.0.0"
            if options_for_name.get("delivery_profile") in {"iphone", "android"}:
                suffix = Path(str(out.get("filename") or "file.bin")).suffix or ".bin"
            else:
                suffix = ".zip" if options_for_name.get("pack_format") in {"zip", "zip-aes256", None} else ".7z"
            out["public_name_preview"] = f"{stem}-v{version}-x64{suffix}"
        secret = out.pop("secret", {})
        out["archive_password"] = secret.get("archive_password") if reveal_secret else None
        options = dict(out.get("options") or {})
        options.pop("local_path", None)
        options.pop("local_root", None)
        out["options"] = options
        out.pop("artifact_path", None)
        stage = store.latest_stage(str(out.get("id")))
        if out.get("state") == "READY":
            out.update({"current_stage": "RECEIVE", "stage_state": "READY", "queue_position": None,
                        "waiting_reason": "等待取回确认"})
        elif stage:
            queue_position = store.stage_queue_position(int(stage["id"]))
            waiting_reason = None
            if stage["state"] == "READY":
                waiting_reason = f"等待 {stage['stage']} 槽位"
            elif stage["state"] == "PAUSED":
                waiting_reason = "任务已暂停"
            out.update({"current_stage": stage["stage"], "stage_state": stage["state"],
                        "queue_position": queue_position, "waiting_reason": waiting_reason})
        else:
            out.update({"current_stage": None, "stage_state": None, "queue_position": None, "waiting_reason": None})
        return out



    @app.get("/health")
    def health():
        usage = shutil.disk_usage(settings.data_dir)
        flash = service.flash.health_details()
        return {
            "ok": True,
            "service": "p20-transfer-hub",
            "version": __version__,
            "capabilities": {
                "pipeline": {
                    "fetch_concurrency": settings.fetch_concurrency,
                    "process_concurrency": settings.process_concurrency,
                    "delivery_concurrency": settings.delivery_concurrency,
                },
                "receiver": {"ticket_schema": "p20.receiver-ticket/v2"},
            },
            "tools": available_tools(),
            "qq_flash": {
                "configured": settings.qqxfer_token_file.exists(),
                **flash,
            },
            "cmcloud": cmcloud_provider.status(),
            "runtime": (runtime_agent.status() or {}).get("status", {}) or {"state": "unknown"},
            "disk": {"total": usage.total, "used": usage.used, "free": usage.free},
        }

    def present_watcher(task: dict) -> dict:
        out = dict(task)
        job = store.get_job(str(task.get("last_job_id") or "")) if task.get("last_job_id") else None
        out["last_job_state"] = job.get("state") if job else None
        out["last_job_number_label"] = (f"P20-{job['job_number']:06d}" if job and job.get("job_number") is not None else None)
        return out

    @app.get("/api/v1/watchers", dependencies=[Depends(require_auth)])
    def list_watchers():
        return [present_watcher(task) for task in store.list_watch_tasks()]

    @app.get("/api/v1/watcher-runs", dependencies=[Depends(require_auth)])
    def list_watcher_runs(watcher_id: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
        if watcher_id and not store.get_watch_task(watcher_id):
            raise HTTPException(404, "watcher not found")
        runs = store.list_watch_runs(watcher_id, limit)
        for run in runs:
            job = store.get_job(str(run.get("job_id") or "")) if run.get("job_id") else None
            run["job_state"] = job.get("state") if job else None
            run["job_number_label"] = (f"P20-{job['job_number']:06d}" if job and job.get("job_number") is not None else None)
        return runs

    @app.post("/api/v1/watchers/test-notification", dependencies=[Depends(require_auth)])
    def test_watcher_notification():
        webhook = WatcherWebhook.from_env(settings.data_dir)
        if not webhook.configured():
            raise HTTPException(503, "watcher webhook is not configured")
        try:
            result = webhook.send(build_test_payload(settings.public_url or webhook.public_url))
        except WatcherNotifyError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"ok": True, "status": result.get("status"), "target": result.get("target")}

    @app.post("/api/v1/watchers/{watcher_id}/enqueue", dependencies=[Depends(require_auth)], status_code=202)
    def enqueue_watcher_job(watcher_id: str, payload: WatcherEnqueue):
        task = store.get_watch_task(watcher_id)
        if not task or not task.get("enabled"):
            raise HTTPException(404, "watcher not found or disabled")
        run = store.get_watch_run(payload.watch_run_id)
        if not run or run.get("watcher_id") != watcher_id or run.get("outcome") != "RUNNING":
            raise HTTPException(409, "watch run is not active for this watcher")
        try:
            validate_public_http_url(payload.source)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        config = task.get("config") or {}
        job = service.create_url_job({
            "source": payload.source,
            "source_type": "url",
            "filename": payload.filename,
            "title": payload.title,
            "delivery_backend": config.get("delivery_backend", "qq_flash"),
            "delivery_profile": config.get("delivery_profile", "unified"),
            "ttl_seconds": int(config.get("ttl_seconds", 172800)),
            "ack_policy": config.get("ack_policy", "required"),
            "scheduled": True,
            "watcher_id": watcher_id,
            "watcher_name": task.get("name"),
            "watch_run_id": payload.watch_run_id,
            "watch_version": payload.version,
            "watch_notify": bool(config.get("notify_ready", True)),
        })
        store.event(job["id"], "job.watcher", {
            "watcher_id": watcher_id, "watcher_name": task.get("name"),
            "watch_run_id": payload.watch_run_id, "version": payload.version,
        })
        return present(job, reveal_secret=True)

    @app.get("/api/v1/runtime/status", dependencies=[Depends(require_auth)])
    def runtime_status():
        return runtime_agent.status()

    @app.post("/api/v1/runtime/start", dependencies=[Depends(require_auth)])
    def runtime_start():
        return runtime_agent.start()

    @app.post("/api/v1/runtime/stop", dependencies=[Depends(require_auth)])
    def runtime_stop():
        return runtime_agent.stop()

    def cmcloud_call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except CMCloudError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.get("/api/v1/cmcloud/status", dependencies=[Depends(require_auth)])
    def cmcloud_status():
        return cmcloud_provider.status()

    @app.post("/api/v1/cmcloud/refresh", dependencies=[Depends(require_auth)])
    def cmcloud_refresh():
        changed = cmcloud_call(cmcloud_provider.refresh, force=True)
        return {"refreshed": bool(changed), **cmcloud_provider.status()}

    @app.get("/api/v1/cmcloud/list", dependencies=[Depends(require_auth)])
    def cmcloud_list(parent_id: str = Query("/", min_length=1, max_length=200), cursor: str = Query("", max_length=1000)):
        data = cmcloud_call(cmcloud_provider.list, parent_id, cursor)
        return data.get("data") or {}

    @app.post("/api/v1/cmcloud/uploads", dependencies=[Depends(require_auth)], status_code=201)
    def cmcloud_create_upload(payload: CMCloudCreateUpload):
        return cmcloud_call(cmcloud.create_upload, **payload.model_dump())

    @app.post("/api/v1/cmcloud/uploads/{session_id}/parts", dependencies=[Depends(require_auth)])
    def cmcloud_issue_parts(session_id: str, payload: CMCloudIssueParts):
        return cmcloud_call(cmcloud.issue_parts, session_id, payload.part_numbers)

    @app.post("/api/v1/cmcloud/uploads/{session_id}/complete", dependencies=[Depends(require_auth)])
    def cmcloud_complete_upload(session_id: str):
        return cmcloud_call(cmcloud.complete, session_id)

    @app.post("/api/v1/cmcloud/downloads/{session_id}", dependencies=[Depends(require_auth)])
    def cmcloud_download(session_id: str):
        return cmcloud_call(cmcloud.download, session_id)

    @app.post("/api/v1/cmcloud/{session_id}/trash", dependencies=[Depends(require_auth)])
    def cmcloud_trash(session_id: str):
        return cmcloud_call(cmcloud.trash, session_id)

    @app.post("/api/v1/jobs", dependencies=[Depends(require_auth)], status_code=202)
    def create_job(payload: CreateJob):
        try:
            source, command_engine = _source_from_input(payload.source)
            from .uav import media_code
            if not media_code(source):
                validate_public_http_url(source)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        values = payload.model_dump(exclude={"source"})
        # A downloader name pasted by the user is a hint, not a hard override.
        # Auto resolution must stay enabled so protected-source/browser fallback can run.
        if command_engine:
            values["command_engine_hint"] = command_engine
        job = service.create_url_job({**values, "source": source})
        return present(job, reveal_secret=True)

    @app.get("/api/v1/local-files", dependencies=[Depends(require_auth)])
    def list_local_files(path: str = Query(default="", max_length=4096)):
        if not path:
            entries = []
            for alias, root in local_file_roots.items():
                if not root.exists() or not root.is_dir():
                    continue
                entries.append({
                    "name": alias,
                    "path": alias,
                    "type": "directory",
                    "size": None,
                    "modified_at": root.stat().st_mtime,
                })
            return {"path": "", "parent": None, "entries": entries}
        try:
            alias, root, directory = resolve_local_file(path)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if not directory.is_dir():
            raise HTTPException(400, "selected path is not a directory")
        entries = []
        for entry in sorted(directory.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            try:
                resolved = entry.resolve(strict=True)
                if not resolved.is_relative_to(root):
                    continue
                stat = resolved.stat()
            except (FileNotFoundError, OSError):
                continue
            child = Path(alias) / resolved.relative_to(root)
            entries.append({
                "name": entry.name,
                "path": child.as_posix(),
                "type": "directory" if resolved.is_dir() else "file",
                "size": stat.st_size if resolved.is_file() else None,
                "modified_at": stat.st_mtime,
            })
        current = Path(alias) if directory == root else Path(alias) / directory.relative_to(root)
        parent = "" if current == Path(alias) else current.parent.as_posix()
        return {"path": current.as_posix(), "parent": parent, "entries": entries}

    @app.post("/api/v1/local-files/jobs", dependencies=[Depends(require_auth)], status_code=202)
    def create_local_file_job(payload: CreateLocalFileJob):
        try:
            alias, root, selected = resolve_local_file(payload.path, require_file=True)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        values = payload.model_dump(exclude={"path"})
        display_path = (Path(alias) / selected.relative_to(root)).as_posix()
        job = service.create_local_job(selected, selected.name, {
            **values,
            "local_origin": "vps",
            "local_root": str(root),
            "local_display_path": display_path,
        })
        return present(job, reveal_secret=True)

    @app.get("/api/v1/jobs", dependencies=[Depends(require_auth)])
    def list_jobs(limit: int = Query(default=100, ge=1, le=500)):
        return [present(x) for x in store.list_jobs(limit)]

    @app.delete("/api/v1/jobs/history", dependencies=[Depends(require_auth)])
    def clear_history():
        return service.clear_terminal_history()

    @app.delete("/api/v1/jobs/{job_id}/history", dependencies=[Depends(require_auth)])
    def clear_history_job(job_id: str):
        try:
            return service.clear_history_job(job_id)
        except KeyError:
            raise HTTPException(404, "job not found")
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.get("/api/v1/jobs/{job_id}/events", dependencies=[Depends(require_auth)])
    def get_job_events(job_id: str, limit: int = Query(default=30, ge=1, le=200)):
        if not store.get_job(job_id):
            raise HTTPException(404, "job not found")
        recent = store.recent_events(job_id, limit)
        messages = store.recent_events(job_id, 20, "job.download_message")
        return sorted({event["id"]: event for event in recent + messages}.values(), key=lambda event: event["id"])

    @app.get("/api/v1/jobs/{job_id}", dependencies=[Depends(require_auth)])
    def get_job(job_id: str, reveal_secret: bool = False):
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        return present(job, reveal_secret=reveal_secret)

    @app.get("/api/v1/jobs/{job_id}/receiver-ticket", dependencies=[Depends(require_auth)])
    def get_receiver_ticket(job_id: str, request: Request):
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        try:
            return build_receiver_ticket(job, settings.public_url or str(request.base_url))
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    def receiver_waited_seconds(job_id: str, job: dict) -> int:
        stamp = str(job.get("updated_at") or job.get("created_at") or "")
        for event in reversed(store.recent_events(job_id, 50, "job.state")):
            if (event.get("data") or {}).get("state") == "READY":
                stamp = str(event.get("created_at") or stamp)
                break
        try:
            ready_at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if ready_at.tzinfo is None:
                ready_at = ready_at.replace(tzinfo=timezone.utc)
            return max(0, int((datetime.now(timezone.utc) - ready_at.astimezone(timezone.utc)).total_seconds()))
        except (TypeError, ValueError):
            return 0

    def receiver_retry_after(waited_seconds: int) -> int:
        if waited_seconds < 10:
            return 1
        if waited_seconds < 60:
            return 15
        if waited_seconds < 300:
            return 30
        return 60

    def try_runtime_receiver_url(job_id: str, item: dict, artifact: int) -> tuple[dict | None, dict]:
        source = item.get("source") or {}
        file_set_id = str(source.get("file_set_id") or "")
        context = {
            "attempted": False,
            "available": False,
            "state": "unknown",
            "error": None,
        }
        if not file_set_id:
            context["error"] = "receiver ticket has no file_set_id"
            return None, context

        health = service.flash.health_details()
        if health.get("healthy"):
            context.update({"available": True, "state": "running"})
            # Receiver URL generation is real QQ activity. Refresh the runtime
            # idle lease so the autostop timer cannot race health -> download-url.
            if runtime_agent.configured():
                runtime_agent.start(timeout=5)
        else:
            runtime = runtime_agent.status() or {}
            runtime_status = runtime.get("status") or {}
            state = runtime_status.get("state")
            context["state"] = state or "unavailable"
            if state in {"stopped", "starting"}:
                context.update({"attempted": True, "available": True})
                store.event(job_id, "job.receiver_resolve", {
                    "status": "WAKING_QQ_RUNTIME", "artifact": artifact, "runtime_state": state,
                })
                started = runtime_agent.start(timeout=15) or {}
                started_status = started.get("status") or {}
                context["state"] = started_status.get("state") or context["state"]
                if context["state"] != "running":
                    context["error"] = started.get("error") or "QQ Runtime did not become ready"
                    return None, context
            elif state == "running":
                context.update({"attempted": True, "available": True})
            else:
                context["error"] = runtime.get("error") or health.get("error") or "QQ Runtime is not available on this node"
                return None, context

        try:
            resolved = service.flash.download_url(file_set_id, item["name"], item["size"], artifact)
        except (DeliveryError, OSError) as exc:
            # qqxfer-api may disappear between a healthy probe and the direct
            # URL request when the runtime is entering idle sleep. That is a
            # fast-path miss, not a Receiver failure: fall back to qfile.
            context["attempted"] = True
            context["error"] = str(exc)
            context["state"] = (runtime_agent.status().get("status") or {}).get("state") or context["state"]
            return None, context
        context.update({"attempted": True, "available": True, "state": "running", "error": None})
        return resolved, context

    @app.get("/api/v1/jobs/{job_id}/receiver-url", dependencies=[Depends(require_auth)])
    def get_receiver_url(job_id: str, response: Response, request: Request, artifact: int = Query(0, ge=0, le=99)):
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        try:
            ticket = build_receiver_ticket(job, settings.public_url or str(request.base_url))
            item = ticket["artifacts"][artifact]
        except (ValueError, IndexError) as exc:
            raise HTTPException(409, str(exc))

        resolved, runtime_context = try_runtime_receiver_url(job_id, item, artifact)
        if resolved:
            store.event(job_id, "job.receiver_resolve", {
                "status": "RECEIVER_READY", "artifact": artifact, "resolver": "qq_runtime",
            })
            response.headers["Cache-Control"] = "no-store"
            return {
                "status": "RECEIVER_READY",
                "resolver": "qq_runtime",
                "url": resolved["url"],
                "expires_at": resolved.get("expire_timestamp_seconds") or None,
                "name": item["name"], "size": item["size"], "sha256": item["sha256"],
            }

        from p20_receive import ReceiverError, _fetch_qfile_items, _pick_qfile_item, _signed_download_url
        try:
            source = item["source"]
            qitem = _pick_qfile_item(_fetch_qfile_items(source["share_link"]), item["name"], item["size"])
            url = _signed_download_url(qitem)
        except ReceiverError as exc:
            waited = receiver_waited_seconds(job_id, job)
            retry_after = receiver_retry_after(waited)
            runtime_unavailable = not runtime_context.get("available")
            if runtime_unavailable:
                message = (
                    "QQ 文件已经上传完成，但公开下载地址仍在同步。当前节点没有可用的 QQ Runtime，"
                    "因此只能等待 QQ 公共分享层完成同步。"
                )
            else:
                message = (
                    "QQ 文件已经上传完成。QQ Runtime 直链暂时未能生成，且公开下载地址仍在同步，"
                    "P20 已退回公共分享链路等待。"
                )
            detail = {
                "status": "WAITING_FOR_QQ_SHARE",
                "code": "qq_public_share_not_ready",
                "reason": "qq_public_share_not_ready",
                "message": message,
                "technical_reason": str(exc),
                "retryable": True,
                "retry_after": retry_after,
                "waited_seconds": waited,
                "runtime": runtime_context,
                "alternatives": [
                    {
                        "action": "retry_later",
                        "message": "按 retry_after 秒后重试；无需重新创建任务、重新下载源文件或重新上传 QQ。",
                    },
                    {
                        "action": "use_runtime_capable_node",
                        "message": "如果有可用的 QQ Runtime 节点，可由该节点按 fileSetId 立即生成临时直链。",
                    },
                ],
            }
            store.event(job_id, "job.receiver_resolve", {
                "status": detail["status"], "artifact": artifact, "waited_seconds": waited,
                "retry_after": retry_after, "runtime_state": runtime_context.get("state"),
                "reason": str(exc),
            })
            raise HTTPException(503, detail, headers={"Retry-After": str(retry_after)}) from exc

        store.event(job_id, "job.receiver_resolve", {
            "status": "RECEIVER_READY", "artifact": artifact, "resolver": "qq_public_share",
        })
        response.headers["Cache-Control"] = "no-store"
        return {
            "status": "RECEIVER_READY",
            "resolver": "qq_public_share",
            "url": url,
            "name": item["name"], "size": item["size"], "sha256": item["sha256"],
        }

    @app.post("/api/v1/jobs/{job_id}/pause", dependencies=[Depends(require_auth)])
    def pause(job_id: str):
        try:
            return present(service.pause(job_id))
        except KeyError:
            raise HTTPException(404, "job not found")
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/v1/jobs/{job_id}/resume", dependencies=[Depends(require_auth)])
    def resume(job_id: str):
        try:
            return present(service.resume(job_id))
        except KeyError:
            raise HTTPException(404, "job not found")
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/v1/jobs/{job_id}/cancel", dependencies=[Depends(require_auth)])
    def cancel(job_id: str):
        try:
            return present(service.cancel(job_id))
        except KeyError:
            raise HTTPException(404, "job not found")

    @app.post("/api/v1/jobs/{job_id}/retry", dependencies=[Depends(require_auth)])
    def retry(job_id: str):
        try:
            return present(service.retry(job_id))
        except KeyError:
            raise HTTPException(404, "job not found")
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/v1/jobs/{job_id}/retry-with-strategy", dependencies=[Depends(require_auth)])
    def retry_with_strategy(job_id: str, payload: RetryStrategy):
        try: return present(service.retry_with_strategy(job_id, payload.strategy))
        except KeyError: raise HTTPException(404, "job not found")
        except ValueError as exc: raise HTTPException(409, str(exc))

    @app.get("/api/v1/skills", dependencies=[Depends(require_auth)])
    def list_skills(state: str | None = None):
        if state and state not in {"candidate", "validated", "stable", "degraded"}: raise HTTPException(400, "invalid state")
        return service.skills.store.list_skills(state)

    @app.post("/api/v1/skills", dependencies=[Depends(require_auth)], status_code=201)
    def create_skill(payload: dict):
        try: return service.skills.create(payload)
        except ValueError as exc: raise HTTPException(400, str(exc))

    @app.get("/api/v1/skills/{skill_id}", dependencies=[Depends(require_auth)])
    def get_skill(skill_id: str):
        result = service.skills.store.get_skill(skill_id)
        if not result: raise HTTPException(404, "skill not found")
        return result

    @app.post("/api/v1/skills/{skill_id}/promote", dependencies=[Depends(require_auth)])
    def promote_skill(skill_id: str, payload: PromoteSkill):
        try: return service.skills.promote(skill_id, payload.state)
        except KeyError: raise HTTPException(404, "skill not found")
        except ValueError as exc: raise HTTPException(409, str(exc))

    @app.post("/api/v1/jobs/{job_id}/apply-skill", dependencies=[Depends(require_auth)])
    def apply_skill(job_id: str, payload: ApplySkill):
        job = store.get_job(job_id)
        if not job: raise HTTPException(404, "job not found")
        try: return present(service.skills.apply(payload.skill_id, job, explicit=True))
        except KeyError: raise HTTPException(404, "skill not found")
        except ValueError as exc: raise HTTPException(409, str(exc))

    @app.get("/api/v1/failure-fingerprints", dependencies=[Depends(require_auth)])
    def get_fingerprints(limit: int = Query(100, ge=1, le=500)):
        return store.list_fingerprints(limit)

    @app.get("/api/v1/failure-fingerprints/{fingerprint_id}", dependencies=[Depends(require_auth)])
    def get_fingerprint(fingerprint_id: str):
        result = store.get_fingerprint(fingerprint_id)
        if not result: raise HTTPException(404, "failure fingerprint not found")
        return result

    @app.post("/api/v1/resolve", dependencies=[Depends(require_auth)])
    def resolve_analysis(payload: dict):
        source = str(payload.get("source", ""))
        try:
            validate_public_http_url(source)
            skill = service.skills.matching(source, str(payload.get("source_type", "auto")))
            conf = skill["config"] if skill else {}
            plan = analyze_source(str(payload.get("source_type", "auto")), source, payload.get("filename"), {},
                                  conf.get("engine", "auto"), conf.get("resolver", "auto"))
            return {"resolver": plan.resolver, "engine": plan.engine_label, "items": [{"engine": x.engine, "kind": x.kind, "filename": x.filename} for x in plan.items],
                    "matched_skill": skill["id"] if skill else None, "metadata": {"header_names": plan.metadata.get("header_names", [])}}
        except ValueError as exc: raise HTTPException(400, str(exc))
        except Exception as exc: return {"status": "analysis_failed", "error_class": type(exc).__name__, "fingerprint": fingerprint({"source": source, "source_type": payload.get("source_type", "auto")}, error=exc)["fingerprint"]}

    @app.get("/api/v1/ui-context", dependencies=[Depends(require_auth)])
    def ui_context(job_id: str | None = None):
        try: return service.ui_context(job_id)
        except KeyError: raise HTTPException(404, "job not found")

    @app.post("/api/v1/jobs/{job_id}/revoke-share", dependencies=[Depends(require_auth)])
    def revoke_share(job_id: str, payload: RevokeShareRequest):
        try: return service.revoke_share(job_id, payload.confirm_irreversible)
        except KeyError: raise HTTPException(404, "job not found")

    @app.post("/api/v1/jobs/{job_id}/ack", dependencies=[Depends(require_auth)])
    def ack(job_id: str):
        try:
            return present(service.ack(job_id))
        except KeyError:
            raise HTTPException(404, "job not found")
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/v1/uploads", dependencies=[Depends(require_auth)], status_code=202)
    async def upload(
        request: Request,
        x_filename: str = Header(...),
        pack: bool = Query(True),
        encrypt: bool = Query(True),
        pack_format: Literal["zip-aes256", "zip", "7z"] = Query("zip-aes256"),
        delivery_backend: Literal["qq_flash", "none"] = Query("qq_flash"),
        delivery_profile: Literal["unified", "desktop", "iphone", "android"] = Query("unified"),
        ttl_seconds: int = Query(172800, ge=300, le=604800),
        ack_policy: Literal["required", "none"] = Query("required"),
        volume_size: str | None = Query(None),
        x_archive_password: str | None = Header(default=None),
    ):
        filename = safe_filename(x_filename)
        if x_archive_password is not None and not (8 <= len(x_archive_password) <= 200):
            raise HTTPException(400, "archive password must be 8-200 characters")
        target = settings.data_dir / "uploads" / f"{uuid.uuid4().hex}-{filename}"
        size = 0
        try:
            with target.open("wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    f.write(chunk)
            if size == 0:
                target.unlink(missing_ok=True)
                raise HTTPException(400, "empty upload")
            job = service.create_local_job(target, filename, {
                "pack": pack,
                "encrypt": encrypt,
                "pack_format": pack_format,
                "delivery_backend": delivery_backend,
                "delivery_profile": delivery_profile,
                "ttl_seconds": ttl_seconds,
                "ack_policy": ack_policy,
                "volume_size": volume_size,
                "password": x_archive_password,
            })
            return present(job, reveal_secret=True)
        except Exception:
            if not target.exists() or size == 0:
                target.unlink(missing_ok=True)
            raise

    @app.get("/api/v1/events", dependencies=[Depends(require_auth)])
    async def events(
        request: Request,
        after_id: int = Query(default=0, ge=0),
        job_id: str | None = None,
    ):
        async def stream():
            cursor = after_id
            while not await request.is_disconnected():
                rows = store.events_after(cursor, job_id=job_id)
                if rows:
                    for row in rows:
                        cursor = row["id"]
                        payload = {
                            "id": row["id"],
                            "job_id": row["job_id"],
                            "type": row["type"],
                            "data": row["data"],
                            "created_at": row["created_at"],
                        }
                        yield f"id: {cursor}\nevent: {row['type']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                else:
                    yield ": keepalive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream")

    web_dist = Path(__file__).resolve().parent / "web_dist"
    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")

    return app
