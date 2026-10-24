#!/usr/bin/env python3
"""Renew only the isolated #126 clone session before its 24h JWT expires.

This is an L2 preparatory credential maintenance probe, never a Gate0 signoff.
Never logs tokens, follows redirects, or contacts a non-loopback origin.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from p2_canary_guard import secure_canary_read, verify_live_canary


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def token_claims(token: str) -> dict:
    """Decode only for preflight identity/expiry; server validates signatures."""
    if not isinstance(token, str) or len(token) > 16000:
        raise ValueError("invalid JWT size")
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("invalid JWT shape")
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "==="))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid JWT claims") from exc
    if not isinstance(payload, dict):
        raise ValueError("JWT claims are not an object")
    return payload


def _expiry(claims: dict) -> float:
    value = claims.get("exp")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 1e12:
        raise ValueError("invalid JWT expiry")
    return float(value)


def _response(opener, request):
    with opener.open(request, timeout=15) as response:
        if response.status != 200:
            raise ValueError("canary session endpoint rejected request")
        raw = response.read(131073)
        if len(raw) > 131072:
            raise ValueError("canary session response exceeds cap")
        return json.loads(raw)


def ensure_fresh_session(root: Path, *, guard=verify_live_canary, opener=None,
                         now: float | None = None, renew_before: int = 7200) -> dict:
    root = Path(root)
    guard(root)  # effective clone API/worker, binary, DB/Redis/storage/loopback first
    now = time.time() if now is None else now
    lock = root / "session-renew.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077 or st.st_nlink != 1:
            raise ValueError("unsafe clone session lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another clone session rotation is in progress") from exc

        path = root / "session.json"
        if os.lstat(path).st_nlink != 1:
            raise ValueError("clone session must not have hardlinks")
        session = json.loads(secure_canary_read(root, path))
        if not isinstance(session, dict):
            raise ValueError("invalid clone session")
        old = token_claims(session.get("token"))
        ref = token_claims(session.get("refresh_token"))
        if (old.get("type") != "access" or ref.get("type") != "refresh"
                or not isinstance(old.get("user_id"), str) or not old["user_id"]
                or ref.get("user_id") != old["user_id"]
                or not isinstance(old.get("tenant_id"), str) or not old["tenant_id"]):
            raise ValueError("clone session identity/type mismatch")
        expiry = _expiry(old)
        if expiry > now + renew_before:
            return {"result": "PASS_CANARY_SESSION_FRESH_ONLY", "rotated": False,
                    "access_expires_at_utc": dt.datetime.fromtimestamp(expiry, dt.timezone.utc).isoformat(),
                    "formal_soak_started": False}
        if _expiry(ref) <= now + 60:
            raise ValueError("clone refresh credential expired")

        kb = json.loads((root / "test-kb.json").read_text())["kb_id"]
        if not isinstance(kb, str) or len(kb) > 100 or not kb:
            raise ValueError("invalid clone KB identity")
        opener = build_opener(NoRedirect()) if opener is None else opener

        # Allocate the private replacement before server-side refresh revokes
        # the previous refresh token; minimize the non-atomic network/file window.
        tmpfd, tmpname = tempfile.mkstemp(prefix=".session-renew-", dir=root)
        try:
            os.fchmod(tmpfd, 0o600)
            req = Request("http://127.0.0.1:18082/api/v1/auth/refresh",
                          data=json.dumps({"refreshToken": session["refresh_token"]}).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
            data = _response(opener, req)
            if not isinstance(data, dict) or data.get("success") is not True:
                raise ValueError("clone refresh did not succeed")
            new_access = data.get("access_token")
            new_refresh = data.get("refresh_token")
            current = token_claims(new_access)
            next_ref = token_claims(new_refresh)
            if (current.get("type") != "access" or next_ref.get("type") != "refresh"
                    or current.get("user_id") != old["user_id"]
                    or next_ref.get("user_id") != old["user_id"]
                    or current.get("tenant_id") != old["tenant_id"]
                    or _expiry(current) <= now + 3600
                    or _expiry(next_ref) <= _expiry(current)):
                raise ValueError("rotated clone credential changed identity or lifetime")

            session["token"] = new_access
            session["refresh_token"] = new_refresh
            with os.fdopen(tmpfd, "w", encoding="utf-8") as output:
                tmpfd = -1
                json.dump(session, output, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(tmpname, path)
            tmpname = ""
            dirfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)

            # Verify the new credential against the exact clone API. If the
            # probe fails, retain the newly rotated refresh token on disk.
            probe = Request("http://127.0.0.1:18082/api/v1/datasource?kb_id=" + quote(kb, safe=""),
                            headers={"Authorization": "Bearer " + new_access}, method="GET")
            if not isinstance(_response(opener, probe), list):
                raise ValueError("new clone session did not authorize its KB")
            expiry = _expiry(current)
            return {"result": "PASS_CANARY_SESSION_FRESH_ONLY", "rotated": True,
                    "access_expires_at_utc": dt.datetime.fromtimestamp(expiry, dt.timezone.utc).isoformat(),
                    "formal_soak_started": False}
        finally:
            if tmpfd >= 0:
                os.close(tmpfd)
            if tmpname:
                os.unlink(tmpname)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(ensure_fresh_session(args.state_dir), sort_keys=True))
    except Exception as exc:
        print(json.dumps({"result": "FAIL_CANARY_SESSION_RENEW", "error_type": type(exc).__name__,
                          "formal_soak_started": False}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
