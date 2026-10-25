#!/usr/bin/env python3
"""P16R2 Vela-only 15-day synthetic NVR soak; no real-NVR access or RAW deletion."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen
from zoneinfo import ZoneInfo

START, END = date(2026, 10, 22), date(2026, 11, 5)
CORPUS_START = START - timedelta(days=1)
ZONE = ZoneInfo("Asia/Singapore")
ROOT = Path(os.environ.get("P16_VELA_UAT_ROOT", "/home/ubuntu/p16-vela-uat")).resolve()
STATE, CORPUS, ARCHIVE, EVIDENCE = (ROOT / p for p in ("state", "corpus", "archive", "evidence"))
DB = STATE / "p16-uat.sqlite3"
SOURCE_REVISION = os.environ.get("P16_SOURCE_REVISION", "unknown")
os.environ["HOME_VISION_DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["HOME_VISION_DATA_ROOT"] = str(STATE / "data")
os.environ["HOME_VISION_ARCHIVE_HEARTBEAT_PATH"] = str(STATE / "archive-worker-heartbeat.json")
os.environ["HOME_VISION_ENVIRONMENT"] = "synthetic-uat"
os.environ["HOME_VISION_ARCHIVE_AUTO_ENQUEUE"] = "false"  # opt-in only in isolated in-process mock call
os.environ["HOME_VISION_NVR_TIMEZONE_VERIFIED"] = "false"  # never enable real scanner

CAMERAS = (("101", "Sim Front Door"), ("102", "Sim Courtyard"))
MAX_BYTES = 512 * 1024 * 1024
MIN_FREE = 6 * 1024**3


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def usage_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def resource_guard() -> dict:
    usage = shutil.disk_usage(ROOT)
    occupied = sum(usage_bytes(x) for x in (STATE, CORPUS, ARCHIVE, EVIDENCE) if x.exists())
    if usage.free < MIN_FREE:
        raise RuntimeError(f"HOST_FREE_DISK_BELOW_UAT_GUARD: {usage.free}")
    if occupied > MAX_BYTES:
        raise RuntimeError(f"UAT_QUOTA_EXCEEDED: {occupied}")
    return {"host_free_bytes": usage.free, "uat_bytes": occupied, "uat_cap_bytes": MAX_BYTES}


def video(day: date, track_id: str) -> bytes:
    path = CORPUS / day.isoformat() / f"camera-{track_id}.mp4"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        seed = int(hashlib.sha256(f"{day}:{track_id}".encode()).hexdigest()[:6], 16)
        color = f"0x{seed:06x}"
        frequency = 300 + seed % 600
        subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", f"color=c={color}:s=320x240:r=10:d=3",
             "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=8000:duration=3",
             "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-movflags", "+faststart", str(path)],
            timeout=60, check=True, capture_output=True,
        )
        path.chmod(0o440)  # original synthetic input is retained, never auto-deleted
    return path.read_bytes()


def corpus(today: date):
    from tests.support.mock_hikvision_nvr import MockRecording
    clips = []
    day = CORPUS_START
    while day <= today:
        for track, _name in CAMERAS:
            # Recorded at a fixed Singapore local time; converted to canonical UTC PTS/source time.
            start = datetime.combine(day, dtime(9, 0), ZONE).astimezone(timezone.utc)
            clips.append(MockRecording(track, start, start + timedelta(seconds=3), video(day, track)))
        day += timedelta(days=1)
    return clips


def setup_db(session):
    from app.models import CameraTrack, NvrDevice, StorageVolume
    nvr = session.get(NvrDevice, "p16-vela-synthetic")
    if nvr is None:
        nvr = NvrDevice(id="p16-vela-synthetic", name="SYNTHETIC UAT ONLY",
                        host="127.0.0.1", timezone="Asia/Singapore")
        session.add(nvr)
        session.flush()
    for track, label in CAMERAS:
        if session.get(CameraTrack, f"p16-sim-{track}") is None:
            session.add(CameraTrack(id=f"p16-sim-{track}", nvr_id=nvr.id, track_id=track,
                                    camera_name=label))
    volume = session.get(StorageVolume, "p16-sim-volume")
    if volume is None:
        # The P16 production storage selector requires >=100GiB reserve. This is
        # VIRTUAL fixture capacity, never host disk truth; resource_guard checks actual host.
        session.add(StorageVolume(id="p16-sim-volume", node_id="synthetic-uat",
                                  mount_path_runtime=str(ARCHIVE), total_bytes=2 * 1024**4,
                                  free_bytes=2 * 1024**4, online=True, writable=True))
    session.commit()
    return nvr


def run_day(today: date) -> dict:
    from sqlalchemy import select
    from app.db import Base, SessionLocal, engine
    from app.domain.nvr_scan import scan_camera_range
    from app.domain.auto_archive import enqueue_closed_day_backlog, closed_day_cutoff_utc
    from app.domain.archive_queue import retry_due_archive_jobs
    from app.infra.nvr.hikvision import HikvisionIsapiClient
    from app.infra.media.ffprobe import FfprobeMediaProbe
    from app.models import ArchiveJob, CameraTrack, LocalAsset, SourceRecording
    from scripts import archive_worker
    from tests.support.mock_hikvision_nvr import MockHikvisionNvr

    Base.metadata.create_all(engine)
    clips = corpus(today)
    # Introduce bounded 453 faults every 4th day, including the baseline day.
    inject_453 = ((today - START).days % 4 == 0)
    with SessionLocal() as session:
        nvr = setup_db(session)
        with MockHikvisionNvr(clips, download_statuses=[453] if inject_453 else []) as mock:
            with HikvisionIsapiClient(base_url=mock.base_url, username=mock.username,
                                     password=mock.password, timeout_seconds=10,
                                     download_method="POST") as client:
                query_start = datetime.combine(CORPUS_START, dtime.min, ZONE).astimezone(timezone.utc)
                query_end = datetime.combine(today + timedelta(days=1), dtime.min, ZONE).astimezone(timezone.utc)
                scanned = 0
                for _ in range(2):
                    for track, _label in CAMERAS:
                        camera = session.get(CameraTrack, f"p16-sim-{track}")
                        res = scan_camera_range(session, nvr=nvr, camera=camera, searcher=client,
                                                query_start=query_start, query_end=query_end,
                                                scan_id=f"vela-{uuid.uuid4().hex}",
                                                scan_min_date=CORPUS_START)
                        if res.state != "success":
                            raise RuntimeError(f"NVR_SCAN_FAILED: {res.error_code}")
                        scanned += res.discovered_count
                        session.commit()
                auto = enqueue_closed_day_backlog(session, nvr_id=nvr.id, now=utc_now(),
                                                  enabled=True, timezone_verified=True)
                session.commit()
                repeated = enqueue_closed_day_backlog(session, nvr_id=nvr.id, now=utc_now(),
                                                      enabled=True, timezone_verified=True)
                session.commit()
                if repeated.queue_result and repeated.queue_result.queued:
                    raise AssertionError("DUPLICATE_AUTO_ENQUEUE")
                attempts = 0
                errors = []
                while attempts < 72:
                    attempts += 1
                    job_id = archive_worker.process_one(session, client, probe=FfprobeMediaProbe(),
                                                       worker_id="vela-sim-worker")
                    if job_id is None:
                        break
                    job = session.get(ArchiveJob, job_id)
                    if job.state == "failed":
                        errors.append(job.error_code)
                        retry_due_archive_jobs(session, now=utc_now() + timedelta(hours=6))
                        session.commit()
                else:
                    raise RuntimeError("WORKER_ATTEMPTS_EXCEEDED")
                if inject_453 and "NVR_BANDWIDTH_LIMIT" not in errors:
                    raise AssertionError("FAULT_453_NOT_OBSERVED")
                cutoff = closed_day_cutoff_utc(now=utc_now(), timezone_name=nvr.timezone)
                sources = session.scalars(select(SourceRecording).where(
                    SourceRecording.nvr_id == nvr.id)).all()
                jobs = session.scalars(select(ArchiveJob)).all()
                assets = session.scalars(select(LocalAsset)).all()
                job_source_ids = {j.source_recording_id for j in jobs}
                expected = 2 * (today - CORPUS_START).days
                if len(assets) != expected:
                    raise AssertionError(f"VERIFIED_ASSET_COUNT {len(assets)} != {expected}")
                if len({a.source_recording_id for a in assets}) != len(assets):
                    raise AssertionError("DUPLICATE_ASSET_FOR_SOURCE")
                for source in sources:
                    closed = source.source_end.replace(tzinfo=timezone.utc) <= cutoff
                    if not closed and source.id in job_source_ids:
                        raise AssertionError("TODAY_FOOTAGE_WAS_ENQUEUED")
                    if closed and source.id not in {a.source_recording_id for a in assets}:
                        raise AssertionError("CLOSED_SOURCE_NOT_VERIFIED")
                file_hashes = {}
                for asset in assets:
                    p = ARCHIVE / asset.relative_path
                    if asset.verification_state != "verified" or not p.is_file():
                        raise AssertionError(f"ASSET_NOT_VERIFIED:{asset.id}")
                    digest = hashlib.sha256(p.read_bytes()).hexdigest()
                    if digest != asset.sha256:
                        raise AssertionError(f"ASSET_HASH_MISMATCH:{asset.id}")
                    file_hashes[asset.id] = digest
                return {"day": today.isoformat(), "scanned_rows": scanned,
                        "source_count": len(sources), "completed_jobs": sum(j.state == "completed" for j in jobs),
                        "asset_count": len(assets), "expected_assets": expected,
                        "today_not_queued": True, "duplicate_enqueues": 0,
                        "download_requests": mock.download_count, "scan_requests": mock.search_count,
                        "fault_453_injected": inject_453, "fault_codes": errors,
                        "archive_hash_set": hashlib.sha256(json.dumps(file_hashes, sort_keys=True).encode()).hexdigest()}


def regression() -> dict:
    implementation = Path(__file__).resolve().parents[1]
    tests = ("tests/test_mock_nvr_acceptance.py", "tests/test_auto_archive.py",
             "tests/test_archive_worker_recovery.py", "tests/test_archive_audit.py",
             "tests/test_scan_nvr.py")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--disable-warnings", *tests],
        cwd=implementation, capture_output=True, text=True, timeout=240,
        env={**os.environ, "PYTHONPATH": str(implementation)},
    )
    return {"exit_code": proc.returncode, "tail": (proc.stdout + proc.stderr)[-2200:]}


def probe() -> dict:
    try:
        with urlopen("http://127.0.0.1:18216/health", timeout=3) as r:
            api = {"code": r.status, "body": r.read(1000).decode("utf-8", errors="replace")}
    except Exception as exc:
        api = {"code": 0, "error": type(exc).__name__}
    return {"timestamp_utc": utc_now().isoformat(), "api": api, "resource": resource_guard()}


def execute(mode: str) -> int:
    for p in (STATE, CORPUS, ARCHIVE, EVIDENCE):
        p.mkdir(parents=True, exist_ok=True)
    with (ROOT / ".single-writer.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if mode == "probe":
            status = probe()
            append_jsonl(EVIDENCE / "hourly-health.jsonl", status)
            return 0 if status["api"]["code"] == 200 else 2
        day = datetime.now(ZONE).date()
        if not START <= day <= END:
            print(f"SKIP_OUTSIDE_WINDOW {day} ({START}..{END})")
            return 0
        stamp = utc_now().strftime("%Y%m%dT%H%M%S%fZ")
        report = {"schema": "p16.vela-soak-day.v1", "date_sgt": day.isoformat(),
                  "timestamp_utc": utc_now().isoformat(), "source_revision": SOURCE_REVISION,
                  "fixture_only": True, "run_id": stamp, "status": "FAIL"}
        started = time.monotonic()
        try:
            report["preflight"] = resource_guard()
            report["business"] = run_day(day)
            report["regression"] = regression()
            if report["regression"]["exit_code"] != 0:
                raise RuntimeError("REGRESSION_FAILURE")
            report["host"] = probe()
            if report["host"]["api"]["code"] != 200:
                raise RuntimeError("P16_API_UNHEALTHY")
            report["status"] = "PASS"
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}:{exc}"[:500]
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        atomic_json(EVIDENCE / "runs" / f"{stamp}.json", report)
        atomic_json(EVIDENCE / "daily" / f"{day.isoformat()}.json", report)
        append_jsonl(EVIDENCE / "run-ledger.jsonl", {k: v for k, v in report.items() if k not in {"business", "regression", "host"}})
        print(json.dumps(report, sort_keys=True, ensure_ascii=False, default=str))
        return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["daily", "probe"], default="daily")
    args = parser.parse_args()
    sys.exit(execute(args.mode))
