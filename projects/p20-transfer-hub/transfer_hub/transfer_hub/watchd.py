from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .db import Store, utcnow
from .watcher_notify import ReadyNotifier, WatcherWebhook
from .watchers import WatchCandidate, WatchRunner, sleep_until_next_tick

DEFAULT_TASKS = [
    {
        "id": "signal-release-apk",
        "name": "Signal Android APK",
        "kind": "github_release",
        "interval_seconds": 300,
        "config": {
            "url": "https://api.github.com/repos/tevriqorg/Signal/releases?per_page=10",
            "asset_suffix": ".apk",
            "include_prerelease": True,
            "title_template": "Signal {version} · Android APK",
            "delivery_profile": "unified",
            "delivery_backend": "qq_flash",
            "ttl_seconds": 172800,
            "ack_policy": "required",
            "bootstrap_current": True,
            "notify_ready": True,
        },
    },
    {
        "id": "chatgpt-desktop-arm64",
        "name": "ChatGPT Desktop arm64",
        "kind": "sparkle_appcast",
        "interval_seconds": 900,
        "config": {
            "url": "https://persistent.oaistatic.com/codex-app-prod/appcast.xml",
            "url_contains": "ChatGPT-darwin-arm64",
            "arch": "arm64",
            "title_template": "ChatGPT Desktop {version} · macOS arm64",
            "filename_template": "ChatGPT-{version}-arm64.zip",
            "delivery_profile": "unified",
            "delivery_backend": "qq_flash",
            "ttl_seconds": 172800,
            "ack_policy": "required",
            "bootstrap_current": True,
            "notify_ready": True,
        },
    },
]


class HubClient:
    def __init__(self, base_url: str, store: Store, data_dir: Path):
        self.base_url = base_url.rstrip("/")
        self.store = store
        self.data_dir = data_dir

    @staticmethod
    def _github_repo(config: dict) -> str:
        parsed = urllib.parse.urlparse(str(config.get("url") or ""))
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.hostname != "api.github.com" or len(parts) < 3 or parts[0] != "repos":
            raise RuntimeError("host_gh watcher requires a GitHub REST releases URL")
        return f"{parts[1]}/{parts[2]}"

    def _mark_watcher_job(self, job: dict, task: dict, candidate: WatchCandidate, run_id: int) -> dict:
        config = task.get("config") or {}
        options = dict(job.get("options") or {})
        options.update({
            "scheduled": True,
            "watcher_id": task["id"],
            "watcher_name": task.get("name"),
            "watch_run_id": run_id,
            "watch_version": candidate.version,
            "watch_notify": bool(config.get("notify_ready", True)),
        })
        updated = self.store.update(job["id"], title=candidate.title, options=options)
        self.store.event(updated["id"], "job.watcher", {
            "watcher_id": task["id"],
            "watcher_name": task.get("name"),
            "watch_run_id": run_id,
            "version": candidate.version,
        })
        if updated.get("job_number") is not None:
            updated["job_number_label"] = f"P20-{updated['job_number']:06d}"
        return updated

    def _enqueue_host_gh(self, task: dict, candidate: WatchCandidate, run_id: int) -> dict:
        config = task.get("config") or {}
        asset_id = candidate.details.get("asset_id")
        if asset_id is None:
            raise RuntimeError("host_gh candidate is missing asset_id")
        repo = self._github_repo(config)
        spool = self.data_dir / "watcher-private"
        spool.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = spool / f"{uuid.uuid4().hex}.asset"
        try:
            with target.open("wb") as output:
                try:
                    subprocess.run(
                        [
                            "gh", "api", "--method", "GET",
                            "-H", "Accept: application/octet-stream",
                            f"repos/{repo}/releases/assets/{int(asset_id)}",
                        ],
                        check=True, stdout=output, stderr=subprocess.PIPE, timeout=900,
                    )
                except FileNotFoundError as exc:
                    raise RuntimeError("host_gh runner requires gh") from exc
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError("host_gh asset download timed out") from exc
                except subprocess.CalledProcessError as exc:
                    detail = (exc.stderr or b"").decode("utf-8", "replace")[:600]
                    raise RuntimeError(f"host_gh asset download failed: {detail or 'unknown gh error'}") from exc

            expected_size = candidate.details.get("size")
            actual_size = target.stat().st_size
            if expected_size is not None and actual_size != int(expected_size):
                raise RuntimeError(f"host_gh asset size mismatch: expected {expected_size}, got {actual_size}")
            digest = str(candidate.details.get("digest") or "")
            if digest.startswith("sha256:"):
                hasher = hashlib.sha256()
                with target.open("rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        hasher.update(chunk)
                if hasher.hexdigest().lower() != digest.split(":", 1)[1].lower():
                    raise RuntimeError("host_gh asset SHA-256 mismatch")

            query = urllib.parse.urlencode({
                "pack": "true",
                "encrypt": "true",
                "pack_format": "zip-aes256",
                "delivery_backend": config.get("delivery_backend", "qq_flash"),
                "delivery_profile": "unified",
                "ttl_seconds": int(config.get("ttl_seconds", 172800)),
                "ack_policy": config.get("ack_policy", "required"),
            })
            completed = subprocess.run(
                [
                    "curl", "--fail-with-body", "--silent", "--show-error",
                    "--max-time", "900", "--request", "POST",
                    "--header", f"X-Filename: {candidate.filename}",
                    "--data-binary", f"@{target}",
                    f"{self.base_url}/api/v1/uploads?{query}",
                ],
                check=False, capture_output=True, timeout=930,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).decode("utf-8", "replace")[:800]
                raise RuntimeError(f"P20 private watcher upload failed: {detail}")
            job = json.loads(completed.stdout)
            return self._mark_watcher_job(job, task, candidate, run_id)
        finally:
            target.unlink(missing_ok=True)

    def enqueue(self, task: dict, candidate: WatchCandidate, run_id: int) -> dict:
        config = task.get("config") or {}
        if config.get("runner") == "host_gh":
            return self._enqueue_host_gh(task, candidate, run_id)
        body = {
            "watch_run_id": run_id,
            "source": candidate.source,
            "filename": candidate.filename,
            "title": candidate.title,
            "version": candidate.version,
            "details": candidate.details,
        }
        req = urllib.request.Request(
            f"{self.base_url}/api/v1/watchers/{task['id']}/enqueue",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "P20-Watchd/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:800]
            raise RuntimeError(f"P20 enqueue HTTP {exc.code}: {detail}") from exc


def main() -> int:
    data_dir = Path(os.getenv("P20_TRANSFER_DATA", "/home/ubuntu/p20-transfer")).resolve()
    base_url = os.getenv("P20_WATCHER_HUB_URL", "http://100.84.55.11:17610")
    store = Store(data_dir / "transfer.db")
    for task in DEFAULT_TASKS:
        store.seed_watch_task(task)
    # Normalize externally registered watchers too, never overwrite unrelated config.
    for task in store.list_watch_tasks():
        config = dict(task["config"])
        if config.get("ttl_seconds") != 172800:
            config["ttl_seconds"] = 172800
            store.update_watch_task(task["id"], config=config)

    client = HubClient(base_url, store, data_dir)
    runner = WatchRunner(store, client.enqueue)
    notifier = ReadyNotifier(
        store,
        WatcherWebhook.from_env(data_dir),
        retry_seconds=int(os.getenv("P20_NOTIFY_RETRY_SECONDS", "60")),
    )
    from .retention import sweep_jobs, sweep_orphan_spool
    spool_dir = Path(os.environ["P20_QQ_SPOOL_ROOT"]).resolve() if os.environ.get("P20_QQ_SPOOL_ROOT") else None
    last_sweep = 0.0
    stopped = False

    def stop(*_args):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(json.dumps({"event": "watchd.started", "at": utcnow(), "tasks": len(DEFAULT_TASKS)}), flush=True)
    while not stopped:
        if time.monotonic() - last_sweep >= 60:
            try:
                summary = sweep_jobs(store, data_dir)
                summary["spool_orphans"] = sweep_orphan_spool(spool_dir, store)
                if any(summary.values()):
                    print(json.dumps({"event": "retention.sweep", **summary}), flush=True)
            except Exception as exc:
                print(json.dumps({"event": "retention.error", "error": str(exc)[:300]}), flush=True)
            last_sweep = time.monotonic()
        for task in store.due_watch_tasks(utcnow()):
            result = runner.run(task)
            print(json.dumps({
                "event": "watch.run", "watcher": task["id"], "outcome": result["outcome"],
                "message": result.get("message"), "job_id": result.get("job_id"),
            }, ensure_ascii=False), flush=True)
            if stopped:
                break
        for notice in notifier.scan():
            print(json.dumps({"event": "watch.notify", **notice}, ensure_ascii=False), flush=True)
        if not stopped:
            sleep_until_next_tick(10)
    print(json.dumps({"event": "watchd.stopped", "at": utcnow()}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
