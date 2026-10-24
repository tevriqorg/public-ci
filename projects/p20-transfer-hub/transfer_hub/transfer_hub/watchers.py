from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .db import Store

SPARKLE = "http://www.andymatuschak.org/xml-namespaces/sparkle"


@dataclass(frozen=True)
class WatchCandidate:
    key: str
    title: str
    version: str
    source: str
    filename: str
    details: dict[str, Any]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(30, seconds))).isoformat()


def _request_bytes(url: str, *, accept: str = "*/*", timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={
        "Accept": accept,
        "User-Agent": "P20-Watchd/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        raise RuntimeError(f"source HTTP {exc.code}: {detail}") from exc


def _request_bytes_via_gh(url: str, *, accept: str = "*/*", timeout: int = 30) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "api.github.com":
        raise ValueError("host_gh watcher source must use https://api.github.com")
    endpoint = parsed.path.lstrip("/")
    if parsed.query:
        endpoint += "?" + parsed.query
    try:
        completed = subprocess.run(
            ["gh", "api", "--method", "GET", "-H", f"Accept: {accept}", endpoint],
            check=True, capture_output=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("host_gh runner requires gh") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("host_gh source request timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace")[:600]
        raise RuntimeError(f"host_gh source failed: {detail or 'unknown gh error'}") from exc
    return completed.stdout


def parse_github_release(data: Any, config: dict[str, Any]) -> WatchCandidate:
    if not isinstance(data, list):
        raise ValueError("GitHub releases response is not a list")
    suffix = str(config.get("asset_suffix") or "").lower()
    allow_prerelease = bool(config.get("include_prerelease", True))
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for release in data:
        if release.get("draft") or (release.get("prerelease") and not allow_prerelease):
            continue
        for asset in release.get("assets") or []:
            name = str(asset.get("name") or "")
            if suffix and not name.lower().endswith(suffix):
                continue
            if not asset.get("browser_download_url") or asset.get("id") is None:
                continue
            candidates.append((release, asset))
    if not candidates:
        raise ValueError("no matching release asset")
    candidates.sort(key=lambda pair: (
        pair[0].get("published_at") or pair[0].get("created_at") or "",
        pair[1].get("created_at") or "",
        int(pair[1].get("id") or 0),
    ), reverse=True)
    release, asset = candidates[0]
    version = str(release.get("tag_name") or release.get("name") or release.get("id"))
    title_template = str(config.get("title_template") or "{version}")
    title = title_template.format(version=version, asset=asset.get("name") or "")
    return WatchCandidate(
        key=f"github-asset:{asset['id']}",
        title=title,
        version=version,
        source=str(asset["browser_download_url"]),
        filename=str(asset.get("name") or f"artifact{suffix or '.bin'}"),
        details={
            "release_id": release.get("id"),
            "asset_id": asset.get("id"),
            "digest": asset.get("digest"),
            "size": asset.get("size"),
            "published_at": release.get("published_at"),
            "html_url": release.get("html_url"),
        },
    )


def parse_sparkle_appcast(raw: bytes | str, config: dict[str, Any]) -> WatchCandidate:
    root = ET.fromstring(raw)
    url_contains = str(config.get("url_contains") or "")
    arch = str(config.get("arch") or "")
    items: list[WatchCandidate] = []
    for item in root.iter("item"):
        enclosure = item.find(f"{{{SPARKLE}}}enclosure")
        if enclosure is None:
            enclosure = item.find("enclosure")
        if enclosure is None:
            continue
        url = str(enclosure.get("url") or "")
        if not url or (url_contains and url_contains not in url):
            continue
        hw = item.find(f"{{{SPARKLE}}}hardwareRequirements")
        if arch and hw is not None and arch not in str(hw.text or ""):
            continue
        version_node = item.find(f"{{{SPARKLE}}}shortVersionString")
        build_node = item.find(f"{{{SPARKLE}}}version")
        version = str((version_node.text if version_node is not None else None) or
                      (build_node.text if build_node is not None else None) or "unknown")
        build = str(build_node.text if build_node is not None else version)
        filename_template = str(config.get("filename_template") or "artifact-{version}.bin")
        title_template = str(config.get("title_template") or "{version}")
        items.append(WatchCandidate(
            key=f"sparkle:{build}:{url}",
            title=title_template.format(version=version, build=build),
            version=version,
            source=url,
            filename=filename_template.format(version=version, build=build),
            details={"build": build, "size": enclosure.get("length"), "arch": arch},
        ))
    if not items:
        raise ValueError("no matching Sparkle enclosure")
    return items[0]


def inspect_task(task: dict[str, Any]) -> WatchCandidate:
    config = task.get("config") or {}
    url = str(config.get("url") or "")
    if not url.startswith("https://"):
        raise ValueError("watch source must use https")
    if task["kind"] == "github_release":
        if config.get("runner") == "host_gh":
            raw = _request_bytes_via_gh(url, accept="application/vnd.github+json")
        else:
            raw = _request_bytes(url, accept="application/vnd.github+json")
        payload = json.loads(raw)
        return parse_github_release(payload, config)
    if task["kind"] == "sparkle_appcast":
        return parse_sparkle_appcast(_request_bytes(url, accept="application/xml,text/xml,*/*"), config)
    raise ValueError(f"unsupported watcher kind: {task['kind']}")


class WatchRunner:
    def __init__(self, store: Store, enqueue: Callable[[dict[str, Any], WatchCandidate, int], dict[str, Any]]):
        self.store = store
        self.enqueue = enqueue

    def run(self, task: dict[str, Any]) -> dict[str, Any]:
        run = self.store.start_watch_run(task["id"])
        interval = int(task.get("interval_seconds") or 300)
        next_run = after(interval)
        candidate: WatchCandidate | None = None
        try:
            candidate = inspect_task(task)
            config = task.get("config") or {}
            if not task.get("last_item_key") and bool(config.get("bootstrap_current", True)):
                message = f"基线已记录：{candidate.title}"
                self.store.update_watch_task(
                    task["id"], last_item_key=candidate.key, last_item_title=candidate.title,
                    last_run_at=utcnow(), last_outcome="BOOTSTRAPPED", last_message=message,
                    next_run_at=next_run,
                )
                return self.store.finish_watch_run(
                    run["id"], outcome="BOOTSTRAPPED", item_key=candidate.key,
                    item_title=candidate.title, message=message, details=candidate.details,
                )
            if task.get("last_item_key") == candidate.key:
                message = f"无更新：仍为 {candidate.title}"
                self.store.update_watch_task(
                    task["id"], last_run_at=utcnow(), last_outcome="SKIPPED",
                    last_message=message, next_run_at=next_run,
                )
                return self.store.finish_watch_run(
                    run["id"], outcome="SKIPPED", item_key=candidate.key,
                    item_title=candidate.title, message=message, details=candidate.details,
                )
            job = self.enqueue(task, candidate, int(run["id"]))
            message = f"发现新内容并创建 {job.get('job_number_label') or job.get('id')}"
            self.store.update_watch_task(
                task["id"], last_item_key=candidate.key, last_item_title=candidate.title,
                last_job_id=job.get("id"), last_run_at=utcnow(), last_outcome="CREATED",
                last_message=message, next_run_at=next_run,
            )
            return self.store.finish_watch_run(
                run["id"], outcome="CREATED", item_key=candidate.key,
                item_title=candidate.title, job_id=job.get("id"), message=message,
                details={**candidate.details, "version": candidate.version},
            )
        except Exception as exc:
            message = str(exc)[:500]
            self.store.update_watch_task(
                task["id"], last_run_at=utcnow(), last_outcome="FAILED",
                last_message=message, next_run_at=next_run,
            )
            return self.store.finish_watch_run(
                run["id"], outcome="FAILED",
                item_key=candidate.key if candidate else None,
                item_title=candidate.title if candidate else None,
                message=message,
            )


def sleep_until_next_tick(seconds: float = 10.0) -> None:
    time.sleep(max(1.0, seconds))
