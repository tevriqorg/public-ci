from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .db import Store


class WatcherNotifyError(RuntimeError):
    pass


def _validate_webhook_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WatcherNotifyError("invalid watcher webhook URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise WatcherNotifyError("watcher webhook URL must not contain credentials or fragment")
    if parsed.scheme == "https":
        return
    try:
        addr = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise WatcherNotifyError("plain HTTP watcher webhook must use a literal private address") from exc
    tailnet_v4 = ipaddress.ip_network("100.64.0.0/10")
    tailnet_v6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
    if not (addr.is_loopback or (addr.version == 4 and addr in tailnet_v4) or (addr.version == 6 and addr in tailnet_v6)):
        raise WatcherNotifyError("plain HTTP watcher webhook must stay on loopback or Tailnet")


class WatcherWebhook:
    def __init__(self, url: str, secret_file: Path, public_url: str = ""):
        self.url = url.strip()
        self.secret_file = secret_file
        self.public_url = public_url.rstrip("/")
        if self.url:
            _validate_webhook_url(self.url)

    @classmethod
    def from_env(cls, data_dir: Path | None = None) -> "WatcherWebhook":
        data_dir = data_dir or Path(os.getenv("P20_TRANSFER_DATA", "/var/lib/p20-transfer"))
        default_secret = data_dir / "secrets" / "hermes-p20-watcher.secret"
        return cls(
            os.getenv("P20_NOTIFY_WEBHOOK_URL", ""),
            Path(os.getenv("P20_NOTIFY_WEBHOOK_SECRET_FILE", str(default_secret))),
            os.getenv("P20_TRANSFER_PUBLIC_URL", ""),
        )

    def configured(self) -> bool:
        return bool(self.url and self.secret_file.is_file())

    def _secret(self) -> str:
        try:
            value = self.secret_file.read_text().strip()
        except OSError as exc:
            raise WatcherNotifyError("watcher webhook secret is unavailable") from exc
        if len(value) < 20:
            raise WatcherNotifyError("watcher webhook secret is invalid")
        return value

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.configured():
            raise WatcherNotifyError("watcher webhook is not configured")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        timestamp = str(int(time.time()))
        signed = timestamp.encode() + b"." + body
        signature = hmac.new(self._secret().encode(), signed, hashlib.sha256).hexdigest()
        request_id = f"p20-{uuid.uuid4().hex}"
        req = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "P20-Watcher-Webhook/1.0",
                "X-Webhook-Timestamp": timestamp,
                "X-Webhook-Signature-V2": signature,
                "X-Request-ID": request_id,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise WatcherNotifyError(f"Hermes webhook HTTP {exc.code}: {detail}") from exc
        except OSError as exc:
            raise WatcherNotifyError(f"Hermes webhook unavailable: {exc}") from exc
        try:
            result = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise WatcherNotifyError("Hermes webhook returned invalid JSON") from exc
        if not isinstance(result, dict) or result.get("status") not in {"delivered", "duplicate"}:
            raise WatcherNotifyError(f"Hermes webhook delivery failed: {result!r}")
        return {"request_id": request_id, "status": result.get("status"), "target": result.get("target")}


def _job_label(job: dict[str, Any]) -> str:
    number = job.get("job_number")
    return f"P20-{number:06d}" if isinstance(number, int) else str(job.get("id") or "P20")


def build_ready_payload(task: dict[str, Any], job: dict[str, Any], public_url: str) -> dict[str, Any]:
    options = job.get("options") or {}
    version = str(options.get("watch_version") or "")
    title = str(job.get("title") or job.get("filename") or task.get("name") or task.get("id"))
    label = _job_label(job)
    message_lines = [
        "📦 P20 Watcher 已准备好",
        str(task.get("name") or task.get("id") or "Watcher"),
        title,
        f"{label} · READY",
    ]
    if version:
        message_lines.append(f"版本：{version}")
    if public_url:
        message_lines.append(f"打开 P20 WebUI 取回：{public_url}")
    return {
        "event_type": "p20.watcher.ready",
        "test": False,
        "watcher_id": task.get("id"),
        "watcher_name": task.get("name"),
        "watch_run_id": options.get("watch_run_id"),
        "version": version,
        "job_id": job.get("id"),
        "job_number_label": label,
        "state": job.get("state"),
        "title": title,
        "webui_url": public_url,
        "message": "\n".join(message_lines),
    }


def build_test_payload(public_url: str) -> dict[str, Any]:
    message = "🧪 P20 Webhook 测试\n这是一次测试任务。\nWatcher → P20 → Hermes → 飞书 链路正常。"
    if public_url:
        message += f"\nP20 WebUI：{public_url}"
    return {
        "event_type": "p20.watcher.test",
        "test": True,
        "watcher_id": "webui-test",
        "watcher_name": "P20 WebUI Webhook Test",
        "state": "READY",
        "message": message,
    }


class ReadyNotifier:
    def __init__(self, store: Store, webhook: WatcherWebhook, retry_seconds: int = 60):
        self.store = store
        self.webhook = webhook
        self.retry_seconds = max(10, retry_seconds)
        self._retry_after: dict[str, float] = {}

    def _already_delivered(self, job_id: str) -> bool:
        for event in self.store.recent_events(job_id, 30, "job.watcher_notification"):
            if (event.get("data") or {}).get("status") == "delivered":
                return True
        return False

    def scan(self) -> list[dict[str, Any]]:
        if not self.webhook.configured():
            return []
        results: list[dict[str, Any]] = []
        now = time.time()
        tasks = {str(task.get("id")): task for task in self.store.list_watch_tasks()}
        for job in self.store.list_jobs(500):
            job_id = str(job.get("id") or "")
            options = job.get("options") or {}
            if (
                not job_id
                or job.get("state") != "READY"
                or options.get("watch_notify") is not True
                or now < self._retry_after.get(job_id, 0)
                or self._already_delivered(job_id)
            ):
                continue
            watcher_id = str(options.get("watcher_id") or "")
            task = tasks.get(watcher_id) or {"id": watcher_id, "name": options.get("watcher_name") or watcher_id}
            try:
                response = self.webhook.send(build_ready_payload(task, job, self.webhook.public_url))
                self.store.event(job_id, "job.watcher_notification", {
                    "status": "delivered", "watcher_id": watcher_id,
                    "request_id": response.get("request_id"), "target": response.get("target"),
                })
                self._retry_after.pop(job_id, None)
                results.append({"job_id": job_id, "status": "delivered"})
            except Exception as exc:
                self._retry_after[job_id] = now + self.retry_seconds
                self.store.event(job_id, "job.watcher_notification", {
                    "status": "failed", "watcher_id": watcher_id, "error": str(exc)[:500],
                })
                results.append({"job_id": job_id, "status": "failed", "error": str(exc)[:200]})
        return results
