from __future__ import annotations

import re
import secrets
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse


class StageType(StrEnum):
    RESOLVE = "RESOLVE"
    FETCH = "FETCH"
    PROCESS = "PROCESS"
    DELIVER = "DELIVER"
    RECEIVE = "RECEIVE"
    CLEANUP = "CLEANUP"


class StageState(StrEnum):
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobState(StrEnum):
    QUEUED = "QUEUED"
    FETCHING = "FETCHING"
    VERIFYING = "VERIFYING"
    PACKING = "PACKING"
    ENCRYPTING = "ENCRYPTING"
    FLASH_UPLOADING = "FLASH_UPLOADING"
    READY = "READY"
    RETRIEVED = "RETRIEVED"
    DOWNLOADED = "DOWNLOADED"  # legacy 0.4.x alias/state
    CLEANUP = "CLEANUP"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"


TERMINAL_STATES = {JobState.DONE, JobState.FAILED, JobState.CANCELLED}

ALLOWED_TRANSITIONS = {
    JobState.QUEUED: {JobState.FETCHING, JobState.VERIFYING, JobState.CANCELLED},
    JobState.FETCHING: {JobState.VERIFYING, JobState.FAILED, JobState.CANCELLED, JobState.PAUSED},
    JobState.VERIFYING: {JobState.PACKING, JobState.ENCRYPTING, JobState.FLASH_UPLOADING, JobState.READY, JobState.FAILED, JobState.CANCELLED},
    JobState.PACKING: {JobState.ENCRYPTING, JobState.FLASH_UPLOADING, JobState.READY, JobState.FAILED, JobState.CANCELLED},
    JobState.ENCRYPTING: {JobState.FLASH_UPLOADING, JobState.READY, JobState.FAILED, JobState.CANCELLED},
    JobState.FLASH_UPLOADING: {JobState.READY, JobState.FAILED, JobState.CANCELLED},
    JobState.READY: {JobState.RETRIEVED, JobState.DOWNLOADED, JobState.CLEANUP, JobState.DONE},
    JobState.RETRIEVED: {JobState.CLEANUP, JobState.DONE},
    JobState.DOWNLOADED: {JobState.CLEANUP, JobState.DONE},
    JobState.CLEANUP: {JobState.DONE, JobState.FAILED},
    JobState.FAILED: {JobState.QUEUED},
    JobState.PAUSED: {JobState.FETCHING, JobState.FAILED, JobState.CANCELLED},
    JobState.CANCELLED: set(),
    JobState.DONE: set(),
}


def can_transition(old: str | JobState, new: str | JobState) -> bool:
    old_state, new_state = JobState(old), JobState(new)
    return old_state == new_state or new_state in ALLOWED_TRANSITIONS.get(old_state, set())


def safe_filename(value: str | None, default: str = "download.bin") -> str:
    name = Path(value or default).name.strip() or default
    name = re.sub(r"[^\w.()\[\] -]+", "_", name, flags=re.UNICODE)
    return name[:180] or default


def filename_from_url(url: str) -> str:
    path = urlparse(url).path
    return safe_filename(Path(path).name or "download.bin")


def detect_engine(source_type: str, source: str | None) -> str:
    t = source_type.lower()
    s = (source or "").lower()
    if t == "local":
        return "local"
    if t in {"stream", "m3u8", "hls", "dash", "ism"} or any(x in s for x in (".m3u8", ".mpd", ".ism/manifest")):
        return "nm3u8dl"
    if t in {"video", "web_video", "playlist"}:
        return "yt-dlp"
    if t == "auto":
        video_hosts = (
            "youtube.com/", "youtu.be/", "x.com/", "twitter.com/",
            "bilibili.com/", "b23.tv/", "vimeo.com/", "tiktok.com/",
            "douyin.com/", "instagram.com/",
        )
        if any(host in s for host in video_hosts):
            return "yt-dlp"
    return "aria2"


def random_password(length: int = 32) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))
