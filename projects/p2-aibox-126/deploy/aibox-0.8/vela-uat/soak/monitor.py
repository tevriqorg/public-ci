#!/usr/bin/env python3
"""Read-only hourly Vela UAT preflight/soak sampler (stdlib only).

No secrets, uploads, database writes, restarts, migrations or pruning.
A 15-day acceptance window starts ONLY from a separately documented real
end-to-end PASS and a deliberately installed gate.json with start_utc.
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
import urllib.error
import urllib.request

from gate_guard import validate_gate

API = "http://172.17.0.1:18080/health"
UI = "http://127.0.0.1:18180/"
GRAPH = "http://127.0.0.1:58082/healthz"
SERVICE_NAMES = ("aibox-uat-api", "aibox-uat-worker", "aibox-uat-embedding", "aibox-uat-graphiti")
CONTAINERS = ("aibox-uat-frontend", "aibox-uat-docreader", "aiboxacc107-postgres-1", "aiboxacc107-redis-1", "aiboxacc107-neo4j-1")


def run(*args):
    p = subprocess.run(args, capture_output=True, text=True, check=False, timeout=8)
    return p.returncode, p.stdout.strip()


def get(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "aibox-soak-v1"}), timeout=6) as r:
            body = r.read(4096)
            if url == API:
                return r.status == 200 and json.loads(body).get("status") == "ok"
            if url == GRAPH:
                return r.status == 200 and json.loads(body).get("status") in ("ready", "ok")
            return r.status == 200
    except (OSError, ValueError, urllib.error.HTTPError):
        return False


def sample(gate_file):
    now = dt.datetime.now(dt.timezone.utc)
    status = {"sample_time_utc": now.isoformat(timespec="seconds"), "checks": {}, "mode": "preflight"}
    for key, url in (("api", API), ("frontend", UI), ("graphiti", GRAPH)):
        status["checks"][key] = get(url)
    for name in SERVICE_NAMES:
        code, output = run("systemctl", "--user", "is-active", name)
        status["checks"][name] = code == 0 and output == "active"
    for name in CONTAINERS:
        code, output = run("docker", "inspect", "--format", "{{json .State}}", name)
        try:
            state = json.loads(output) if code == 0 else {}
            health = state.get("Health", {}).get("Status")
            ok = bool(state.get("Running")) and health in (None, "healthy")
        except ValueError:
            ok = False
        status["checks"][name] = ok
    volume = os.statvfs("/")
    status["disk_available_gib"] = round(volume.f_bavail * volume.f_frsize / (1024**3), 2)
    mem = {}
    with open("/proc/meminfo", encoding="ascii") as fh:
        for line in fh:
            field, _, amount = line.partition(":")
            if field in ("MemAvailable", "SwapFree"):
                mem[field] = int(amount.strip().split()[0]) / (1024**2)
    status["mem_available_gib"] = round(mem.get("MemAvailable", 0), 2)
    status["swap_free_gib"] = round(mem.get("SwapFree", 0), 2)
    status["checks"]["disk_reserve_5gib"] = status["disk_available_gib"] >= 5
    status["checks"]["memory_reserve_1gib"] = status["mem_available_gib"] >= 1
    gate, gate_reason = validate_gate(gate_file, now=now)
    if gate:
        start = dt.datetime.fromisoformat(gate["start_utc"].replace("Z", "+00:00"))
        status["mode"] = "soak"
        status["trial_id"] = gate["trial_id"]
        status["soak_elapsed_days"] = round((now - start).total_seconds() / 86400, 3)
        status["fifteen_days_elapsed"] = (now - start) >= dt.timedelta(days=15)
    elif gate_file.exists():
        status["checks"]["valid_soak_gate"] = False
        status["gate_error"] = gate_reason
    # preflight remains safe when no approved trial exists
    status["all_healthy"] = all(status["checks"].values())
    return status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", type=pathlib.Path, required=True)
    args = ap.parse_args()
    root = args.state_dir.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    snap = sample(root / "gate.json")
    output = root / "samples.jsonl"
    with output.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(snap, sort_keys=True, separators=(",", ":")) + "\n")
    os.chmod(output, 0o600)
    print(json.dumps({"time": snap["sample_time_utc"], "healthy": snap["all_healthy"], "mode": snap["mode"], "failed": [k for k,v in snap["checks"].items() if not v]}))
    raise SystemExit(0 if snap["all_healthy"] else 1)


if __name__ == "__main__":
    main()
