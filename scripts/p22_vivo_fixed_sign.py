#!/usr/bin/env python3
"""Fail-closed signer for P22's EXISTING vivo APK certificate; no key generation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

EXPECTED_CERT = "30ba94601c871ba5c776febbcb3acf35b20259f3272493dd73ac5cb07741c4c2"
EXPECTED_PACKAGE = "tech.bogomolov.incomingsmsgateway"
MIN_VERSION_CODE = 18


def fail(message):
    raise SystemExit("P22 fixed-signing gate FAILED: " + message)


def tool(args):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        # Private signing file paths/passwords and tool stderr must never be echoed into a public Action log.
        fail("required Android signing/inspection command failed (details deliberately suppressed)")
    return result.stdout


def identity(apk):
    badging = tool(["aapt", "dump", "badging", str(apk)])
    m = re.search(r"^package: name='([^']+)' versionCode='(\d+)'", badging, re.MULTILINE)
    if not m or m.group(1) != EXPECTED_PACKAGE or int(m.group(2)) < MIN_VERSION_CODE:
        fail("APK package/version incompatible with existing vivo installation")
    if "application-debuggable" in badging:
        fail("refusing to sign debuggable APK")
    return int(m.group(2))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--source-sha", required=True)
    p.add_argument("--signing-dir", required=True, type=Path)
    p.add_argument("--candidate-manifest", required=True, type=Path)
    a = p.parse_args()
    if not re.fullmatch(r"[a-f0-9]{40}", a.source_sha):
        fail("invalid SHA")
    src = a.input.resolve()
    dest = a.output.resolve()
    root = a.signing_dir.resolve()
    if not src.is_file() or src == dest or dest.exists() or root in dest.parents:
        fail("invalid input/output path or output already exists")
    for item in (root / "vivo-test-keystore.p12", root / "keystore.password"):
        if not item.is_file() or item.stat().st_mode & 0o077:
            fail("required existing keystore/password file missing or permissions too broad")
    candidate = json.loads(a.candidate_manifest.read_text())
    if (candidate.get("source_sha") != a.source_sha or
            candidate.get("package") != EXPECTED_PACKAGE or
            candidate.get("status") != "UNSIGNED_ONLY"):
        fail("unsigned candidate does not match build provenance")
    ver = identity(src)
    if candidate.get("version_code") != ver:
        fail("manifest version mismatch")

    os.umask(0o077)
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".p22-signer-", dir=str(dest.parent)) as tmp:
        signed = Path(tmp) / "candidate.apk"
        tool(["apksigner", "sign",
              "--ks", str(root / "vivo-test-keystore.p12"),
              "--ks-type", "PKCS12",
              "--ks-pass", "file:" + str(root / "keystore.password"),
              "--out", str(signed), str(src)])
        cert_output = tool(["apksigner", "verify", "--verbose", "--print-certs", str(signed)])
        certs = [v.lower() for v in re.findall(
            r"Signer #\d+ certificate SHA-256 digest:\s*([0-9a-fA-F]+)", cert_output)]
        if certs != [EXPECTED_CERT]:
            fail("certificate does not match original installed vivo identity")
        if identity(signed) != ver:
            fail("signed APK identity changed")
        digest = hashlib.sha256(signed.read_bytes()).hexdigest()
        signed.rename(dest)
        manifest = {
            "schema": "p22-vivo-fixed-signing/v1",
            "source_repository": "tevriqorg/P22-Personal-Relay",
            "source_sha": a.source_sha,
            "package": EXPECTED_PACKAGE,
            "version_code": ver,
            "certificate_sha256": EXPECTED_CERT,
            "apk_sha256": digest,
            "status": "fixed-signed-candidate; real-device-e2e-not-verified",
        }
        dest.with_suffix(".manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        print(json.dumps(manifest, sort_keys=True))


if __name__ == "__main__":
    main()
