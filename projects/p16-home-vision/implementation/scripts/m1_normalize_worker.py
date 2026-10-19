"""Small stdlib-only worker for the LAN M1 pilot.

Source and destination are mounted through SMB; the A8 API owns task leases.
"""
from __future__ import annotations

import json
import fcntl
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


API = os.environ.get("HOME_VISION_NORMALIZE_API", "http://192.168.50.61:8000/internal/normalize")
HEALTH_API = API.removesuffix("/internal/normalize") + "/health"
SMB_ROOT = Path(os.environ.get("HOME_VISION_SMB_ROOT", "/Volumes/A8_sdb1"))
WORK_ROOT = Path(os.environ.get("HOME_VISION_WORK_ROOT", "/Users/don/home-vision-mba-worker"))
WORKER_ID = "mba-normalize"
PROTOCOL_VERSION = "p16-normalize-mba-v1"
FFMPEG = os.environ.get("FFMPEG", "/opt/homebrew/bin/ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "/opt/homebrew/bin/ffprobe")
CONCURRENCY = min(3, max(1, int(os.environ.get("HOME_VISION_NORMALIZE_CONCURRENCY", "3"))))


def post(path: str, payload: dict, timeout: int = 30) -> dict:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(f"{API}/{path}", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def get_health() -> dict:
    with urllib.request.urlopen(HEALTH_API, timeout=10) as response:
        return json.load(response)


def validate_local(path: Path, duration_ms: int) -> None:
    probe = subprocess.run(
        [FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    payload = json.loads(probe.stdout)
    streams = payload.get("streams") or []
    video = next(stream for stream in streams if stream.get("codec_type") == "video")
    actual_duration_ms = round(float((payload.get("format") or {}).get("duration")) * 1000)
    allowed_delta_ms = max(5000, min(120000, round(duration_ms * 0.02)))
    if abs(actual_duration_ms - duration_ms) > allowed_delta_ms:
        raise RuntimeError("normalized-duration-mismatch")
    if video.get("codec_name") != "hevc" or video.get("codec_tag_string") != "hvc1":
        raise RuntimeError("normalized-video-profile-invalid")
    duration = max(0.0, duration_ms / 1000)
    for offset in (0.0, duration / 2, max(0.0, duration - 1.0)):
        subprocess.run([FFMPEG, "-v", "error", "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err", "-ss", str(offset), "-i", str(path), "-an", "-frames:v", "1", "-f", "null", "-"], check=True, stderr=subprocess.DEVNULL)


def failure_code(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "WORKER_TIMEOUT"
    if isinstance(exc, urllib.error.HTTPError):
        return f"API_HTTP_{exc.code}"
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, TimeoutError):
            return "WORKER_TIMEOUT"
        return "NETWORK_ERROR"
    if isinstance(exc, FileNotFoundError):
        return "SOURCE_FILE_MISSING"
    if isinstance(exc, subprocess.CalledProcessError):
        return f"MEDIA_TOOL_EXIT_{abs(exc.returncode)}"
    if isinstance(exc, RuntimeError):
        return {
            "normalized-duration-mismatch": "NORMALIZED_DURATION_MISMATCH",
            "normalized-video-profile-invalid": "NORMALIZED_VIDEO_PROFILE_INVALID",
            "normalize-worker-protocol-mismatch": "WORKER_PROTOCOL_MISMATCH",
            "p16-release-revision-invalid": "RELEASE_REVISION_INVALID",
        }.get(str(exc), "NORMALIZATION_FAILED")
    return "NORMALIZATION_FAILED"


def process_slot(slot: int) -> None:
    worker_id = f"{WORKER_ID}-{slot}"
    waiting_for_mount = False
    while True:
        if not os.path.ismount(SMB_ROOT):
            if not waiting_for_mount:
                print(f"worker={worker_id} wait=archive-share-unmounted path={SMB_ROOT}", flush=True)
                waiting_for_mount = True
            time.sleep(30)
            continue
        if waiting_for_mount:
            print(f"worker={worker_id} archive-share=mounted", flush=True)
            waiting_for_mount = False
        try:
            health = get_health()
            if health.get("normalize_worker_protocol") != PROTOCOL_VERSION:
                raise RuntimeError("normalize-worker-protocol-mismatch")
            release_revision = health.get("p16_release_revision", "")
            if len(release_revision) != 40 or any(char not in "0123456789abcdef" for char in release_revision):
                raise RuntimeError("p16-release-revision-invalid")
            claim = post("claim", {
                "worker_id": worker_id,
                "release_revision": release_revision,
                "protocol_version": PROTOCOL_VERSION,
            }).get("claim")
        except Exception as exc:
            print(f"claim=retry error={type(exc).__name__}:{exc}", flush=True)
            time.sleep(30)
            continue
        if not claim:
            time.sleep(30)
            continue
        asset_id = claim["asset_id"]
        token = claim["lease_token"]
        local_part = WORK_ROOT / f"{asset_id}.mp4.part"
        source = SMB_ROOT / claim["source_relative_path"]
        remote_part = SMB_ROOT / f"{claim['output_relative_path']}.part"
        remote_final = SMB_ROOT / claim["output_relative_path"]
        stop = threading.Event()

        def heartbeats() -> None:
            while not stop.wait(30):
                try:
                    post("heartbeat", {"asset_id": asset_id, "worker_id": worker_id, "lease_token": token, "phase": "processing"})
                except Exception:
                    pass

        threading.Thread(target=heartbeats, daemon=True).start()
        try:
            if remote_final.exists():
                try:
                    validate_local(remote_final, int(claim["duration_ms"]))
                except Exception:
                    remote_final.unlink()
                else:
                    post("heartbeat", {"asset_id": asset_id, "worker_id": worker_id, "lease_token": token, "phase": "verifying"})
                    post("complete", {"asset_id": asset_id, "worker_id": worker_id, "lease_token": token, "phase": "completed"}, timeout=1800)
                    print(f"asset={asset_id} result=completed-existing", flush=True)
                    continue
            if not source.is_file():
                raise FileNotFoundError(source)
            if not local_part.exists():
                command = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "warning", "-y", "-copyts", "-start_at_zero", "-avoid_negative_ts", "disabled", "-i", str(source), "-map", "0:v:0", "-c:v", "copy", "-tag:v", "hvc1", "-map", "0:a:0?"]
                if (claim.get("source_audio_codec") or "").lower() == "pcm_alaw":
                    command += ["-c:a", "aac", "-profile:a", "aac_low", "-ac", "1", "-ar", "8000", "-b:a", "32k"]
                else:
                    command += ["-c:a", "copy"]
                command += ["-fps_mode", "passthrough", "-movflags", "+faststart", "-f", "mp4", str(local_part)]
                subprocess.run(command, check=True)
            validate_local(local_part, int(claim["duration_ms"]))
            remote_part.parent.mkdir(parents=True, exist_ok=True)
            remote_part.unlink(missing_ok=True)
            shutil.copyfile(local_part, remote_part)
            os.replace(remote_part, remote_final)
            post("heartbeat", {"asset_id": asset_id, "worker_id": worker_id, "lease_token": token, "phase": "verifying"})
            post("complete", {"asset_id": asset_id, "worker_id": worker_id, "lease_token": token, "phase": "completed"}, timeout=1800)
            print(f"asset={asset_id} result=completed", flush=True)
        except Exception as exc:
            try:
                remote_part.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                code = failure_code(exc)
                post("fail", {"asset_id": asset_id, "worker_id": worker_id, "lease_token": token, "phase": "failed", "error_code": code})
            except Exception:
                code = failure_code(exc)
            print(f"asset={asset_id} result=failed error_code={code}", flush=True)
        finally:
            stop.set()
            local_part.unlink(missing_ok=True)


def main() -> None:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    lock = (WORK_ROOT / ".worker.lock").open("w")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("worker=already-running", flush=True)
        return
    print(f"worker=started concurrency={CONCURRENCY}", flush=True)
    with ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="normalize") as pool:
        list(pool.map(process_slot, range(1, CONCURRENCY + 1)))


if __name__ == "__main__":
    main()
