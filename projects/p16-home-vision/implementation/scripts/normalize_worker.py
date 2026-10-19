from __future__ import annotations

import argparse
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from app.db import SessionLocal
from app.domain.normalize_queue import claim_next_normalization, release_normalization_claim, renew_normalization_claim
from scripts.normalize_existing_assets import _normalize_one


HEARTBEAT_PATH = Path("/state/normalize-worker-heartbeat.json")
_heartbeat_state = {"phase": "starting", "selected": 0, "completed": 0, "failed": 0}
_heartbeat_lock = threading.Lock()


def _write_heartbeat() -> None:
    with _heartbeat_lock:
        payload = json.dumps({"heartbeat_at": datetime.now(timezone.utc).isoformat(), **_heartbeat_state}, ensure_ascii=False)
    temporary = HEARTBEAT_PATH.with_name(f".{HEARTBEAT_PATH.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, HEARTBEAT_PATH)
    except OSError:
        temporary.unlink(missing_ok=True)


def _heartbeat(phase: str, selected: int = 0, completed: int = 0, failed: int = 0) -> None:
    with _heartbeat_lock:
        _heartbeat_state.update(phase=phase, selected=selected, completed=completed, failed=failed)
    _write_heartbeat()


def _heartbeat_loop() -> None:
    while True:
        time.sleep(30)
        _write_heartbeat()


def run_once() -> tuple[int, int, int]:
    completed = failed = selected = 0
    while True:
        with SessionLocal() as session:
            claim = claim_next_normalization(session, "a8-normalize")
        if claim is None:
            break
        selected += 1
        _heartbeat("processing", 1, completed, failed)
        print(f"claimed={claim.asset.id} worker=a8-normalize", flush=True)
        stop = threading.Event()

        def renew() -> None:
            while not stop.wait(30):
                with SessionLocal() as session:
                    if not renew_normalization_claim(session, claim.asset.id, "a8-normalize", claim.token, "processing"):
                        return

        threading.Thread(target=renew, daemon=True).start()
        try:
            result = _normalize_one(claim.asset, claim.source, claim.volume)
            completed += 1
            with SessionLocal() as session:
                release_normalization_claim(session, claim.asset.id, "a8-normalize", claim.token, "completed")
            print(f"asset={claim.asset.id} result={result}", flush=True)
        except Exception as exc:
            failed += 1
            with SessionLocal() as session:
                release_normalization_claim(session, claim.asset.id, "a8-normalize", claim.token, "failed")
            print(f"asset={claim.asset.id} result=failed error={type(exc).__name__}:{exc}", flush=True)
        finally:
            stop.set()
        _heartbeat("processing", 1, completed, failed)
    _heartbeat("idle" if not failed else "degraded", selected, completed, failed)
    return selected, completed, failed


def main() -> None:
    parser = argparse.ArgumentParser(description="Durable single-concurrency playback normalization workflow")
    parser.add_argument("--interval-seconds", type=int, default=30)
    args = parser.parse_args()
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    _heartbeat("idle")
    while True:
        run_once()
        time.sleep(max(5, args.interval_seconds))


if __name__ == "__main__":
    main()
