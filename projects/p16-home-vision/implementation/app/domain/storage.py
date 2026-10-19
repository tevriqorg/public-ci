from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.models import StorageVolume


class StorageError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class StoragePolicy:
    minimum_reserve_bytes: int = 100 * 1024**3
    reserve_ratio: float = 0.05

    def reserve_for(self, total_bytes: int) -> int:
        return max(self.minimum_reserve_bytes, int(total_bytes * self.reserve_ratio))


def is_volume_eligible(
    volume: StorageVolume,
    *,
    required_bytes: int,
    policy: StoragePolicy = StoragePolicy(),
) -> bool:
    if not volume.online or not volume.writable:
        return False
    if volume.total_bytes is None or volume.free_bytes is None:
        return False
    reserve = policy.reserve_for(volume.total_bytes)
    return volume.free_bytes - required_bytes >= reserve


def select_storage_volume(
    volumes: Iterable[StorageVolume],
    *,
    required_bytes: int,
    policy: StoragePolicy = StoragePolicy(),
) -> StorageVolume:
    eligible = [v for v in volumes if is_volume_eligible(v, required_bytes=required_bytes, policy=policy)]
    if not eligible:
        raise StorageError("STORAGE_NOT_READY")
    return sorted(eligible, key=lambda v: (-(v.free_bytes or 0), v.id))[0]


def filesystem_smoke_check(directory: Path) -> None:
    """Non-destructive create/fsync/rename/unlink check inside a caller-scoped directory."""
    directory.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    first = directory / f".home-vision-smoke-{token}.tmp"
    second = directory / f".home-vision-smoke-{token}.renamed"
    try:
        fd = os.open(first, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, b"home-vision-storage-smoke\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(first, second)
        with second.open("rb") as handle:
            if handle.read() != b"home-vision-storage-smoke\n":
                raise StorageError("STORAGE_SMOKE_CONTENT_MISMATCH")
        second.unlink()
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError("STORAGE_SMOKE_FAILED") from exc
    finally:
        for candidate in (first, second):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass
