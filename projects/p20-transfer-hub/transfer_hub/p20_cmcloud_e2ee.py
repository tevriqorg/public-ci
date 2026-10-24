#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from transfer_hub.e2ee import E2EEError, decrypt_file, encrypt_file, generate_receiver_keypair


class ClientError(RuntimeError):
    pass


def _atomic_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w") as handle:
        json.dump(data, handle, separators=(",", ":"), sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


class BrokerClient:
    def __init__(self, base_url: str, token_file: Path | None = None, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token_file.read_text().strip() if token_file else ""

    def call(self, method: str, path: str, payload: dict | None = None) -> dict:
        headers = {"Accept": "application/json"}
        data = None
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        req = Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            body = exc.read().decode(errors="replace")[:500]
            raise ClientError(f"broker HTTP {exc.code}: {body}") from exc

    def create_upload(self, size: int, sha256: str) -> dict:
        return self.call("POST", "/api/v1/cmcloud/uploads", {"size": size, "sha256": sha256, "encryption": "e2ee"})

    def issue_parts(self, session_id: str, part_numbers: list[int]) -> dict:
        return self.call("POST", f"/api/v1/cmcloud/uploads/{session_id}/parts", {"part_numbers": part_numbers})

    def complete(self, session_id: str) -> dict:
        return self.call("POST", f"/api/v1/cmcloud/uploads/{session_id}/complete")

    def download_capability(self, session_id: str) -> dict:
        return self.call("POST", f"/api/v1/cmcloud/downloads/{session_id}")

    def trash(self, session_id: str) -> dict:
        return self.call("POST", f"/api/v1/cmcloud/{session_id}/trash")


def _put_part(url: str, source: Path, offset: int, length: int, timeout: float = 180.0) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ClientError("upload capability is not HTTPS")
    conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout)
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    try:
        conn.putrequest("PUT", target)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(length))
        conn.putheader("Origin", "https://yun.139.com")
        conn.putheader("Referer", "https://yun.139.com/")
        conn.endheaders()
        with source.open("rb") as handle:
            handle.seek(offset)
            remaining = length
            while remaining:
                block = handle.read(min(1024 * 1024, remaining))
                if not block:
                    raise ClientError("ciphertext ended before upload part completed")
                conn.send(block)
                remaining -= len(block)
        response = conn.getresponse()
        response.read()
        if not 200 <= response.status < 300:
            raise ClientError(f"EOS PUT failed with HTTP {response.status}")
    finally:
        conn.close()


def upload_ciphertext(ciphertext: Path, broker: BrokerClient, state_path: Path | None = None) -> dict:
    ciphertext = ciphertext.resolve()
    size = ciphertext.stat().st_size
    digest = _sha256(ciphertext)
    state_path = state_path or ciphertext.with_suffix(ciphertext.suffix + ".upload.json")
    state = None
    if state_path.exists():
        try:
            candidate = json.loads(state_path.read_text())
            if candidate.get("ciphertext_sha256") == digest and candidate.get("ciphertext_size") == size:
                state = candidate
        except Exception:
            state = None

    initial_parts: dict[int, str] = {}
    if state is None:
        created = broker.create_upload(size, digest)
        session_id = created["session_id"]
        state = {
            "v": 1,
            "session_id": session_id,
            "ciphertext_path": str(ciphertext),
            "ciphertext_size": size,
            "ciphertext_sha256": digest,
            "part_size": int(created["part_size"]),
            "part_count": int(created["part_count"]),
            "completed_parts": [],
            "state": "uploading",
        }
        for part in created.get("parts") or []:
            initial_parts[int(part["part_number"])] = part["upload_url"]
        if created.get("exist") or (created.get("rapid_upload") and not created.get("parts")):
            state["state"] = "completed"
        _atomic_state(state_path, state)
    else:
        session_id = state["session_id"]

    if state.get("state") == "completed":
        return state

    completed = {int(v) for v in state.get("completed_parts") or []}
    part_size = int(state["part_size"])
    part_count = int(state["part_count"])
    pending = [n for n in range(1, part_count + 1) if n not in completed]

    for batch_start in range(0, len(pending), 100):
        batch = pending[batch_start:batch_start + 100]
        urls = {n: initial_parts[n] for n in batch if n in initial_parts}
        missing = [n for n in batch if n not in urls]
        if missing:
            issued = broker.issue_parts(session_id, missing)
            urls.update({int(p["part_number"]): p["upload_url"] for p in issued.get("parts") or []})
        for number in batch:
            offset = (number - 1) * part_size
            length = min(part_size, size - offset)
            url = urls.get(number)
            if not url:
                raise ClientError(f"broker did not return upload capability for part {number}")
            try:
                _put_part(url, ciphertext, offset, length)
            except ClientError:
                refreshed = broker.issue_parts(session_id, [number])
                parts = refreshed.get("parts") or []
                if not parts:
                    raise
                _put_part(parts[0]["upload_url"], ciphertext, offset, length)
            completed.add(number)
            state["completed_parts"] = sorted(completed)
            _atomic_state(state_path, state)

    broker.complete(session_id)
    state["state"] = "completed"
    _atomic_state(state_path, state)
    return state


def _download_capability_to_file(broker: BrokerClient, session_id: str, destination: Path) -> dict:
    cap = broker.download_capability(session_id)
    expected_size = int(cap.get("size") or 0)
    expected_hash = str(cap.get("sha256") or "")
    offset = destination.stat().st_size if destination.exists() else 0
    if expected_size and offset > expected_size:
        destination.unlink()
        offset = 0

    for attempt in range(2):
        cap = broker.download_capability(session_id) if attempt else cap
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        req = Request(cap["url"], headers=headers)
        try:
            with urlopen(req, timeout=180) as response:
                status = getattr(response, "status", 200)
                mode = "ab" if offset and status == 206 else "wb"
                with destination.open(mode) as out:
                    for block in iter(lambda: response.read(1024 * 1024), b""):
                        out.write(block)
            break
        except HTTPError as exc:
            if attempt == 0 and exc.code in {401, 403, 404}:
                continue
            raise ClientError(f"EOS GET failed with HTTP {exc.code}") from exc
    if expected_size and destination.stat().st_size != expected_size:
        raise ClientError("downloaded ciphertext size mismatch")
    digest = _sha256(destination)
    if expected_hash and digest.lower() != expected_hash.lower():
        raise ClientError("downloaded ciphertext SHA-256 mismatch")
    return cap


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="P20 CMCloud E2EE reference client")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("keygen", help="generate receiver X25519 keypair")
    p.add_argument("--private", type=Path, required=True)
    p.add_argument("--public", type=Path, required=True)

    p = sub.add_parser("encrypt", help="encrypt a source file for a receiver")
    p.add_argument("source", type=Path)
    p.add_argument("--recipient-public", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--chunk-size", type=int, default=8 * 1024 * 1024)

    p = sub.add_parser("decrypt", help="decrypt a downloaded P20 E2EE object")
    p.add_argument("source", type=Path)
    p.add_argument("--private", type=Path, required=True)
    p.add_argument("--output", type=Path)

    p = sub.add_parser("upload", help="upload an already encrypted object through Tokyo Broker")
    p.add_argument("ciphertext", type=Path)
    p.add_argument("--broker", required=True)
    p.add_argument("--api-token-file", type=Path)
    p.add_argument("--state", type=Path)

    p = sub.add_parser("send", help="encrypt then upload through Tokyo Broker")
    p.add_argument("source", type=Path)
    p.add_argument("--recipient-public", type=Path, required=True)
    p.add_argument("--broker", required=True)
    p.add_argument("--api-token-file", type=Path)
    p.add_argument("--work-dir", type=Path, default=Path("."))

    p = sub.add_parser("receive", help="download a session and decrypt it locally")
    p.add_argument("session_id")
    p.add_argument("--private", type=Path, required=True)
    p.add_argument("--broker", required=True)
    p.add_argument("--api-token-file", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--work-dir", type=Path, default=Path("."))
    p.add_argument("--trash", action="store_true", help="trash CMCloud transit object after successful decrypt")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "keygen":
            generate_receiver_keypair(args.private, args.public)
            print(json.dumps({"private": str(args.private), "public": str(args.public)}))
            return 0
        if args.command == "encrypt":
            result = encrypt_file(args.source, args.output, args.recipient_public, chunk_size=args.chunk_size)
            print(json.dumps({"ciphertext": str(result.ciphertext_path), "size": result.ciphertext_size, "sha256": result.ciphertext_sha256, "chunks": result.chunks}))
            return 0
        if args.command == "decrypt":
            result = decrypt_file(args.source, args.output, args.private)
            print(json.dumps({"output": str(result.output_path), "filename": result.filename, "sha256": result.plaintext_sha256}))
            return 0

        broker = BrokerClient(args.broker, args.api_token_file)
        if args.command == "upload":
            state = upload_ciphertext(args.ciphertext, broker, args.state)
            print(json.dumps({"provider": "cmcloud", "encryption": "e2ee", "session_id": state["session_id"], "state": state["state"]}))
            return 0
        if args.command == "send":
            args.work_dir.mkdir(parents=True, exist_ok=True)
            fd, raw_path = tempfile.mkstemp(prefix="p20-e2ee-", suffix=".blob", dir=args.work_dir)
            os.close(fd)
            cipher = Path(raw_path)
            encrypt_file(args.source, cipher, args.recipient_public)
            state = upload_ciphertext(cipher, broker)
            print(json.dumps({"provider": "cmcloud", "encryption": "e2ee", "session_id": state["session_id"], "ciphertext": str(cipher), "state": state["state"]}))
            return 0
        if args.command == "receive":
            args.work_dir.mkdir(parents=True, exist_ok=True)
            cipher = args.work_dir / f"{args.session_id}.p20e2ee.part"
            _download_capability_to_file(broker, args.session_id, cipher)
            result = decrypt_file(cipher, args.output, args.private)
            if args.trash:
                broker.trash(args.session_id)
            print(json.dumps({"output": str(result.output_path), "filename": result.filename, "sha256": result.plaintext_sha256, "trashed": bool(args.trash)}))
            return 0
    except (ClientError, E2EEError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
