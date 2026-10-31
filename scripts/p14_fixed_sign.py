#!/usr/bin/env python3
"""Sign one P14 ARM64 test APK using the existing, pinned Recovery PKCS#12 identity.

Run only in the separate, trusted GitHub Actions signing job. Never commit private keys.
This does not release or install an APK.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

EXPECTED_CERT = "dfa33df904e4eb3ca30fdc61a1817dbec36d131a94a594663f7d79522430cc94"
EXPECTED_PACKAGE = "me.rerere.rikkahub.debug"
EXPECTED_ALIAS = "p14-fixed-recovery"
DEFAULT_SIGNING_DIR = Path("/unconfigured/explicit-signing-dir-required")

def fail(message):
    raise SystemExit(f"P14 fixed-signing gate FAILED: {message}")

def run(command):
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        fail(f"tool {Path(command[0]).name} exited {result.returncode}; stop and inspect locally (no secret output)")
    return result.stdout

def main():
    parser = argparse.ArgumentParser(description="Sign and verify a P14 ARM64 test-only APK")
    parser.add_argument("--input", required=True, type=Path, help="Unsigned / CI-debug-signed arm64 APK")
    parser.add_argument("--output", required=True, type=Path, help="Private output path for fixed-signed APK")
    parser.add_argument("--source-sha", required=True, help="Exact 40-character Git commit SHA for APK source")
    parser.add_argument("--signing-dir", type=Path, default=DEFAULT_SIGNING_DIR)
    args = parser.parse_args()

    if not re.fullmatch(r"[0-9a-f]{40}", args.source_sha):
        fail("source SHA must be an exact lowercase Git SHA")
    src, dest, keydir = args.input.resolve(), args.output.resolve(), args.signing_dir.resolve()
    if src == dest or not src.is_file() or src.suffix.lower() != ".apk" or dest.suffix.lower() != ".apk":
        fail("distinct, existing input APK and output APK paths are required")
    if dest == keydir or keydir in dest.parents:
        fail("never write APK output into the signing-key directory")
    if dest.exists():
        fail("output already exists; never overwrite a signed candidate")
    keystore, password, certificate = (keydir / name for name in
        ("p14-fixed.p12", "keystore.password", "certificate.der"))
    for path in (keystore, password, certificate):
        if not path.is_file():
            fail("required existing signing material missing; do not generate replacement keys")
        if path.stat().st_mode & 0o077:
            fail("signing material has overly broad permissions")
    if hashlib.sha256(certificate.read_bytes()).hexdigest() != EXPECTED_CERT:
        fail("pinned certificate identity mismatch")
    with zipfile.ZipFile(src) as apk:
        if not any(name.startswith("lib/arm64-v8a/") for name in apk.namelist()):
            fail("input APK contains no arm64-v8a libraries")
    package_line = next((line for line in run(["aapt", "dump", "badging", str(src)]).splitlines()
                         if line.startswith("package: ")), "")
    match = re.search(r"name='([^']+)'", package_line)
    if not match or match.group(1) != EXPECTED_PACKAGE:
        fail("APK package does not match the pinned P14 test application ID")

    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if dest.parent.stat().st_mode & 0o077:
        fail("output directory must not be group/world accessible")
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix=".p14-sign-", dir=dest.parent) as workdir:
        signed = Path(workdir) / "candidate.apk"
        run(["apksigner", "sign", "--ks", str(keystore), "--ks-type", "PKCS12",
             "--ks-key-alias", EXPECTED_ALIAS, "--ks-pass", f"file:{password}",
             "--out", str(signed), str(src)])
        verify = run(["apksigner", "verify", "--verbose", "--print-certs", str(signed)])
        certs = re.findall(r"Signer #\d+ certificate SHA-256 digest:\s*([0-9a-fA-F]+)", verify)
        if certs != [EXPECTED_CERT]:
            fail("signed APK certificate is not the one pinned Recovery identity")
        if not signed.is_file() or signed.stat().st_size == 0:
            fail("signed APK missing")
        sha256 = hashlib.file_digest(signed.open("rb"), "sha256").hexdigest()
        manifest = {"schema": "p14.fixed-signing-manifest/v1",
                    "authority": "tevriqorg/P14-Entry#51",
                    "source_sha": args.source_sha, "package": EXPECTED_PACKAGE,
                    "abi": "arm64-v8a", "certificate_sha256": EXPECTED_CERT,
                    "signed_apk_sha256": sha256, "release_status": "test-only-not-published"}
        meta = Path(workdir) / "manifest.json"
        meta.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        shutil.move(str(signed), str(dest))
        shutil.move(str(meta), str(dest.with_suffix(".manifest.json")))
        dest.chmod(0o600)
        dest.with_suffix(".manifest.json").chmod(0o600)
    print(json.dumps(manifest, sort_keys=True))

if __name__ == "__main__":
    main()
