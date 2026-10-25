#!/usr/bin/env python3
"""P14 CI ARM64 -> pinned Tokyo signature -> P20 QQ, test-only.

This is the replacement for the old RikkaHub GitHub Release watcher.
It never publishes a GitHub Release, rebuilds sources, invokes Hermes directly,
changes keys, wipes client/server data or pushes an unsigned APK.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

CI_REPO = "tevriqorg/public-ci"
CI_WORKFLOW = "p14-arm64-candidate.yml"
ARTIFACT_PREFIX = "p14-arm64-UNSIGNED-"
CERT_SHA = "dfa33df904e4eb3ca30fdc61a1817dbec36d131a94a594663f7d79522430cc94"
MIN_RUN = int(os.getenv("P14_CI_MIN_RUN", "40148099245"))
P20_URL = os.getenv("P20_BASE", "http://100.84.55.11:17610").rstrip("/")
P20_TOKEN = Path(os.getenv("P20_TOKEN_FILE", "/home/ubuntu/p20-transfer/api.token"))
ROOT = Path(os.getenv("P14_CANDIDATE_DIR", "/home/ubuntu/p20-transfer/p14-remote/ci-signed-arm64"))
STATE = Path(os.getenv("P14_DELIVERY_STATE", "/home/ubuntu/p20-transfer/p14-remote/p14-ci-delivery-state.json"))
SIGNER = Path(os.getenv("P14_SIGNER", "/home/ubuntu/p20-transfer/p14-remote/recovery-candidate-20261025/p14_sign_fixed_candidate.py"))


def fail(reason: str):
    raise RuntimeError(reason)


def process(args: list[str], *, input_text: str | None = None, timeout: int = 40) -> str:
    p = subprocess.run(args, input=input_text, text=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=timeout, check=False)
    if p.returncode:
        # Don't print stderr: transient CLI errors can echo credentials in command details.
        fail(f"{Path(args[0]).name} returned exit {p.returncode}")
    return p.stdout


def gh_json(endpoint: str) -> dict:
    return json.loads(process(["gh", "api", endpoint], timeout=35))


def eligible_run() -> tuple[dict, dict, str] | None:
    endpoint = f"repos/{CI_REPO}/actions/workflows/{CI_WORKFLOW}/runs?status=success&per_page=12"
    runs = gh_json(endpoint).get("workflow_runs", [])
    for run in sorted(runs, key=lambda r: int(r.get("id", 0)), reverse=True):
        run_id = int(run.get("id") or 0)
        if (run_id < MIN_RUN or run.get("conclusion") != "success"
                or run.get("head_branch") != "main"
                or run.get("event") not in ("push", "workflow_dispatch")):
            continue
        items = gh_json(f"repos/{CI_REPO}/actions/runs/{run_id}/artifacts").get("artifacts", [])
        for artifact in items:
            name = str(artifact.get("name", ""))
            if artifact.get("expired") or not name.startswith(ARTIFACT_PREFIX):
                continue
            sha = name[len(ARTIFACT_PREFIX):]
            if re.fullmatch("[0-9a-f]{40}", sha):
                return run, artifact, sha
    return None


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def json_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_state() -> dict:
    if not STATE.exists():
        return {}
    return json_file(STATE)


def save_state(data: dict):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE.parent, 0o700)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=STATE.parent,
                                     prefix=".p14-ci-", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        json.dump(data, tmp, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.write("\n")
    os.chmod(tmp_path, 0o600)
    tmp_path.replace(STATE)


def signed_candidate(run_id: int, sha: str, artifact_name: str) -> tuple[Path, dict]:
    if not SIGNER.is_file():
        fail("fixed signing gate is missing; refuse fallback debug signatures")
    directory = ROOT / f"run-{run_id}-{sha[:8]}"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    filename = f"RikkaHub-P14R3-TEST-{sha[:8]}-CI{run_id}-arm64.apk"
    final = directory / filename
    manifest_path = final.with_suffix(".manifest.json")
    if not (final.is_file() and manifest_path.is_file()):
        if final.exists() or manifest_path.exists():
            fail("partial previous signing output; refusing automatic overwrite")
        with tempfile.TemporaryDirectory(prefix=".ci-download-", dir=directory) as tmp:
            source_dir = Path(tmp)
            process(["gh", "run", "download", str(run_id), "--repo", CI_REPO,
                     "--name", artifact_name, "--dir", str(source_dir)], timeout=900)
            apk = source_dir / "app-arm64-v8a-UNSIGNED.apk"
            proof = source_dir / "SOURCE_SHA"
            hashes = source_dir / "UNSIGNED-SHA256SUMS"
            if not apk.is_file() or not proof.is_file() or not hashes.is_file():
                fail("CI artifact missing required APK / SHA provenance files")
            if proof.read_text(encoding="utf-8").strip() != sha:
                fail("CI artifact Android source revision mismatch")
            entries = hashes.read_text(encoding="utf-8").strip().split()
            if len(entries) != 2 or not re.fullmatch("[0-9a-f]{64}", entries[0]):
                fail("CI artifact unsigned digest record malformed")
            if file_sha(apk) != entries[0]:
                fail("CI artifact unsigned APK digest mismatch")
            process(["python3", str(SIGNER), "--input", str(apk),
                     "--output", str(final), "--source-sha", sha], timeout=100)
    manifest = json_file(manifest_path)
    if (manifest.get("source_sha") != sha or manifest.get("package") != "me.rerere.rikkahub.debug"
            or manifest.get("abi") != "arm64-v8a" or manifest.get("certificate_sha256") != CERT_SHA
            or manifest.get("signed_apk_sha256") != file_sha(final)
            or manifest.get("release_status") != "test-only-not-published"):
        fail("signed ARM64 candidate manifest/fingerprint/digest mismatch")
    verified = process(["apksigner", "verify", "--verbose", "--print-certs", str(final)])
    if (f"certificate SHA-256 digest: {CERT_SHA}" not in verified
            or "Verified using v2 scheme (APK Signature Scheme v2): true" not in verified
            or "Verified using v3 scheme (APK Signature Scheme v3): true" not in verified):
        fail("fixed-signed ARM64 APK signature verification failed")
    return final, manifest


def p20_auth() -> str:
    token = P20_TOKEN.read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9._~+/-]{16,512}", token):
        fail("invalid P20 authentication secret format")
    return token


def existing_job(filename: str) -> dict | None:
    req = urllib.request.Request(f"{P20_URL}/api/v1/jobs?limit=500",
                                 headers={"Authorization": f"Bearer {p20_auth()}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        jobs = json.load(resp)
    matches = [x for x in jobs if x.get("filename") == filename]
    if len(matches) > 1:
        fail("duplicate P20 jobs already exist for this exact candidate name")
    return matches[0] if matches else None


def p20_enqueue(apk: Path) -> dict:
    existing = existing_job(apk.name)
    if existing:
        return existing
    auth_header = f'header = "Authorization: Bearer {p20_auth()}"\n'
    filename_header = f'header = "X-Filename: {apk.name}"\n'
    # curl config on stdin keeps the secret out of argv, command logs, and disk.
    url = (f"{P20_URL}/api/v1/uploads?delivery_backend=qq_flash"
           f"&delivery_profile=unified&ttl_seconds=172800&ack_policy=required")
    response = process(["curl", "--config", "-", "--fail-with-body", "--silent",
                        "--show-error", "--max-time", "900", "--request", "POST",
                        "--data-binary", f"@{apk}", url],
                       input_text=auth_header + filename_header, timeout=930)
    job = json.loads(response)
    if not re.fullmatch(r"[0-9a-f]{32}", str(job.get("id", ""))):
        fail("P20 upload response did not return a valid job id")
    return job


TAG_JOB_CODE = """
import json, os, sys
from pathlib import Path
from transfer_hub.db import Store
d = json.load(sys.stdin)
store = Store(Path(os.getenv("P20_TRANSFER_DATA", "/var/lib/p20-transfer")) / "transfer.db")
job = store.get_job(d["job_id"])
if not job or job.get("filename") != d["filename"]:
    raise SystemExit("P20 job not found or filename mismatch")
options = dict(job.get("options") or {})
if options.get("watcher_id") not in (None, "", "p14-arm64-ci"):
    raise SystemExit("refusing to retag an unrelated P20 watcher job")
if options.get("watch_run_id") not in (None, "", d["run_id"]):
    raise SystemExit("refusing to alter the job's prior CI run identity")
options.update({"scheduled": True, "watcher_id": "p14-arm64-ci",
                "watcher_name": "P14R3 Android 固定签名真机验收",
                "watch_run_id": d["run_id"],
                "watch_version": "p14r3-" + d["source_sha"][:8],
                "watch_notify": True})
store.update(d["job_id"], options=options)
store.event(d["job_id"], "job.watcher", {"watcher_id": "p14-arm64-ci",
    "watch_run_id": d["run_id"], "version": options["watch_version"]})
print(json.dumps({"job_id": d["job_id"], "watch_notify": True,
                  "state": job["state"]}))
"""


def enable_ready_qq(job: dict, apk: Path, run_id: int, sha: str) -> dict:
    input_obj = json.dumps({"job_id": job["id"], "filename": apk.name,
                            "run_id": run_id, "source_sha": sha})
    output = process(["docker", "exec", "-i", "p20-watchd", "python3", "-c", TAG_JOB_CODE],
                     input_text=input_obj, timeout=35)
    result = json.loads(output)
    if result.get("job_id") != job["id"] or result.get("watch_notify") is not True:
        fail("P20 ready-time QQ notification metadata was not persisted")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="No download, signature, P20 job or QQ")
    args = parser.parse_args()
    match = eligible_run()
    if not match:
        print(json.dumps({"event": "no_successful_arm64_candidate", "min_run": MIN_RUN}))
        return
    run, artifact, sha = match
    run_id = int(run["id"])
    old = load_state()
    if old.get("last_run_id", 0) >= run_id:
        print(json.dumps({"event": "already_queued", "run_id": run_id,
                          "job_id": old.get("last_job_id"), "last_state": old.get("last_state")}))
        return
    if args.dry_run:
        print(json.dumps({"event": "ready_for_delivery", "run_id": run_id,
                          "source_sha": sha, "artifact_id": artifact["id"],
                          "artifact_size": artifact.get("size_in_bytes")}))
        return
    apk, manifest = signed_candidate(run_id, sha, artifact["name"])
    job = p20_enqueue(apk)
    tag = enable_ready_qq(job, apk, run_id, sha)
    state = {"schema": "p14.ci-p20-delivery/v1", "last_run_id": run_id,
             "last_artifact_id": int(artifact["id"]), "last_source_sha": sha,
             "last_signed_sha256": manifest["signed_apk_sha256"], "last_job_id": job["id"],
             "last_state": tag["state"], "queued_at": int(time.time())}
    save_state(state)
    print(json.dumps({"event": "p20_created_or_reused", "run_id": run_id,
                      "source_sha": sha, "job_id": job["id"],
                      "job_state": tag["state"], "watch_notify": True,
                      "fixed_signing": CERT_SHA}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Surface operational error type only; do not echo secrets, token-bearing URLs or traceback.
        print(json.dumps({"event": "p14_ci_delivery_failed", "error_type": type(exc).__name__,
                          "reason": str(exc)[:180] if isinstance(exc, RuntimeError) else "operation failed"},
                         ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
