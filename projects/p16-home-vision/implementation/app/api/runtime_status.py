from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["runtime"])


def _read_int(path: Path) -> int | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value == "max":
            return None
        return int(value)
    except (OSError, ValueError):
        return None


def _memory_status() -> dict[str, int | None]:
    current = _read_int(Path("/sys/fs/cgroup/memory.current"))
    limit = _read_int(Path("/sys/fs/cgroup/memory.max"))
    if current is not None and limit is not None:
        return {"used_bytes": current, "limit_bytes": limit}
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, raw = line.partition(":")
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(raw.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return {
        "used_bytes": (values.get("MemTotal") or 0) - (values.get("MemAvailable") or 0) if values else None,
        "limit_bytes": values.get("MemTotal"),
    }


def _network_status() -> dict[str, int]:
    received = 0
    transmitted = 0
    try:
        for line in Path("/proc/net/dev").read_text(encoding="utf-8").splitlines():
            if ":" not in line:
                continue
            interface, values = line.split(":", 1)
            if interface.strip() == "lo":
                continue
            fields = values.split()
            if len(fields) >= 9:
                received += int(fields[0])
                transmitted += int(fields[8])
    except (OSError, ValueError, IndexError):
        pass
    return {"received_bytes": received, "transmitted_bytes": transmitted}


@router.get("/runtime-status")
def runtime_status() -> dict[str, object]:
    try:
        load_1m = os.getloadavg()[0]
    except OSError:
        load_1m = None
    return {
        "runtime": "container",
        "cpu": {"load_1m": load_1m, "logical_cores": os.cpu_count()},
        "memory": _memory_status(),
        "network": _network_status(),
        "host": {
            "status": "not_collected",
            "reason": "主机级遥测未接入；当前仅采集 Home Vision 容器指标",
        },
        "collected_at": datetime.now(timezone.utc),
    }
