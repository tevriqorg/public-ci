from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

_REFRESH_URL = "https://aas.caiyun.feixin.10086.cn:443/tellin/authTokenRefresh.do"
_ROUTE_URL = "https://user-njs.yun.139.com/user/route/qryRoutePolicy"
_CLIENT_INFO = "||13|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||dW5kZWZpbmVk||"


class CMCloudError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthInfo:
    prefix: str
    account: str
    token: str
    expires_ms: int | None

    @property
    def expires_at(self) -> str | None:
        if not self.expires_ms:
            return None
        return datetime.fromtimestamp(self.expires_ms / 1000, tz=timezone.utc).isoformat()


def decode_authorization(raw: str) -> AuthInfo:
    try:
        decoded = base64.b64decode(raw.strip()).decode()
    except Exception as exc:
        raise CMCloudError("invalid CMCloud authorization encoding") from exc
    parts = decoded.split(":", 2)
    if len(parts) != 3 or not all(parts):
        raise CMCloudError("invalid CMCloud authorization format")
    token_fields = parts[2].split("|")
    expires_ms = None
    if len(token_fields) >= 4:
        try:
            expires_ms = int(token_fields[3])
        except ValueError:
            pass
    return AuthInfo(parts[0], parts[1], parts[2], expires_ms)


def _atomic_text(path: Path, value: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _read_json_response(response: Any) -> dict[str, Any]:
    body = response.read()
    try:
        return json.loads(body)
    except Exception as exc:
        raise CMCloudError(f"CMCloud returned non-JSON response (HTTP {getattr(response, 'status', '?')})") from exc


def _download_expiry(url: str) -> int | None:
    query = parse_qs(urlparse(url).query)
    raw_date = (query.get("X-Amz-Date") or [None])[0]
    raw_ttl = (query.get("X-Amz-Expires") or [None])[0]
    if not raw_date or not raw_ttl:
        return None
    try:
        ts = datetime.strptime(raw_date, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
        return int(ts + int(raw_ttl))
    except (ValueError, TypeError):
        return None


class CMCloudProvider:
    """Minimal reverse-API adapter. Long-lived Authorization never leaves this object."""

    def __init__(self, auth_file: Path, state_file: Path, *, opener: Callable[..., Any] = urlopen, timeout: float = 20.0):
        self.auth_file = auth_file
        self.state_file = state_file
        self.opener = opener
        self.timeout = timeout
        self._host: str | None = None
        self._last_refresh: str | None = None
        self._load_state()

    def configured(self) -> bool:
        return self.auth_file.is_file() and bool(self.auth_file.read_text().strip())

    def _authorization(self) -> str:
        if not self.configured():
            raise CMCloudError("CMCloud credential is not configured")
        return self.auth_file.read_text().strip()

    def auth_info(self) -> AuthInfo:
        return decode_authorization(self._authorization())

    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_file.read_text())
            self._host = str(data.get("personal_host") or "") or None
            self._last_refresh = str(data.get("last_refresh_at") or "") or None
        except Exception:
            pass

    def _save_state(self) -> None:
        data = {"personal_host": self._host, "last_refresh_at": self._last_refresh}
        _atomic_text(self.state_file, json.dumps(data, separators=(",", ":")), 0o600)

    def status(self) -> dict[str, Any]:
        if not self.configured():
            return {"configured": False, "healthy": False}
        try:
            info = self.auth_info()
        except CMCloudError:
            return {"configured": True, "healthy": False, "error": "invalid_credential"}
        remaining = None if not info.expires_ms else max(0, int(info.expires_ms / 1000 - time.time()))
        return {
            "configured": True,
            "healthy": remaining is None or remaining > 0,
            "account": f"{info.account[:3]}****{info.account[-4:]}" if len(info.account) >= 7 else "***",
            "expires_at": info.expires_at,
            "expires_in_seconds": remaining,
            "personal_host": urlparse(self._host).hostname if self._host else None,
            "last_refresh_at": self._last_refresh,
        }

    def refresh(self, *, force: bool = False, skew_seconds: int = 15 * 86400) -> bool:
        info = self.auth_info()
        if not force and info.expires_ms and info.expires_ms / 1000 - time.time() > skew_seconds:
            return False
        payload = f"<root><token>{info.token}</token><account>{info.account}</account><clienttype>656</clienttype></root>".encode()
        req = Request(_REFRESH_URL, data=payload, method="POST", headers={"Content-Type": "application/xml"})
        with self.opener(req, timeout=self.timeout) as response:
            body = response.read()
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise CMCloudError("CMCloud token refresh returned invalid XML") from exc
        if root.findtext("return") != "0" or not root.findtext("token"):
            raise CMCloudError("CMCloud token refresh failed")
        new_auth = base64.b64encode(f"{info.prefix}:{info.account}:{root.findtext('token')}".encode()).decode()
        _atomic_text(self.auth_file, new_auth + "\n", 0o600)
        self._last_refresh = datetime.now(timezone.utc).isoformat()
        self._host = None
        self._save_state()
        return True

    def _json_post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        req = Request(url, data=json.dumps(payload, separators=(",", ":")).encode(), method="POST", headers=headers)
        with self.opener(req, timeout=self.timeout) as response:
            return _read_json_response(response)

    def ensure_host(self) -> str:
        self.refresh()
        if self._host:
            return self._host
        info = self.auth_info()
        payload = {"userInfo": {"userType": 1, "accountType": 1, "accountName": info.account}, "modAddrType": 1}
        data = self._json_post(_ROUTE_URL, payload, {"Authorization": "Basic " + self._authorization(), "Content-Type": "application/json"})
        for item in (data.get("data") or {}).get("routePolicyList") or []:
            if item.get("modName") == "personal" and item.get("httpsUrl"):
                self._host = str(item["httpsUrl"]).rstrip("/")
                self._save_state()
                return self._host
        raise CMCloudError("CMCloud personal API host is unavailable")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": "Basic " + self._authorization(),
            "Content-Type": "application/json",
            "X-Yun-Api-Version": "v1",
            "X-Yun-App-Channel": "10000034",
            "X-Yun-Client-Info": _CLIENT_INFO,
        }

    def post(self, path: str, payload: dict[str, Any], *, retry_auth: bool = True) -> dict[str, Any]:
        host = self.ensure_host()
        data = self._json_post(host + path, payload, self._headers())
        if data.get("success") is False and str(data.get("code")) == "04000005" and retry_auth:
            self.refresh(force=True)
            return self.post(path, payload, retry_auth=False)
        if data.get("success") is False:
            raise CMCloudError(f"CMCloud {path} failed: code={data.get('code')} message={data.get('message')}")
        return data

    @staticmethod
    def part_size(size: int) -> int:
        return 512 * 1024 * 1024 if size > 30 * 1024**3 else 100 * 1024 * 1024

    @staticmethod
    def part_infos(size: int, part_size: int, numbers: list[int] | None = None) -> list[dict[str, Any]]:
        count = max(1, (size + part_size - 1) // part_size)
        selected = numbers or list(range(1, count + 1))
        out = []
        for number in selected:
            if number < 1 or number > count:
                raise CMCloudError("invalid upload part number")
            offset = (number - 1) * part_size
            out.append({"partNumber": number, "partSize": min(part_size, size - offset), "parallelHashCtx": {"partOffset": offset}})
        return out

    def list(self, parent_id: str = "/", cursor: str = "") -> dict[str, Any]:
        return self.post("/file/list", {"imageThumbnailStyleList": ["Small", "Large"], "orderBy": "updated_at", "orderDirection": "DESC", "pageInfo": {"pageCursor": cursor, "pageSize": 100}, "parentFileId": parent_id})

    def create_upload(self, *, parent_id: str, name: str, size: int, sha256: str, part_size: int | None = None) -> dict[str, Any]:
        part_size = part_size or self.part_size(size)
        parts = self.part_infos(size, part_size)
        data = self.post("/file/create", {"contentHash": sha256, "contentHashAlgorithm": "SHA256", "contentType": "application/octet-stream", "parallelUpload": False, "partInfos": parts[:100], "size": size, "parentFileId": parent_id, "name": name, "type": "file", "fileRenameMode": "auto_rename"})["data"]
        return {"file_id": data.get("fileId"), "file_name": data.get("fileName"), "upload_id": data.get("uploadId"), "exist": bool(data.get("exist")), "rapid_upload": bool(data.get("rapidUpload")), "parts": [{"part_number": int(p["partNumber"]), "upload_url": p["uploadUrl"]} for p in (data.get("partInfos") or [])], "part_size": part_size, "part_count": len(parts)}

    def get_upload_urls(self, *, file_id: str, upload_id: str, size: int, part_size: int, part_numbers: list[int]) -> list[dict[str, Any]]:
        info = self.auth_info()
        payload = {"fileId": file_id, "uploadId": upload_id, "partInfos": self.part_infos(size, part_size, part_numbers), "commonAccountInfo": {"account": info.account, "accountType": 1}}
        data = self.post("/file/getUploadUrl", payload)["data"]
        return [{"part_number": int(p["partNumber"]), "upload_url": p["uploadUrl"]} for p in (data.get("partInfos") or [])]

    def complete(self, *, file_id: str, upload_id: str, sha256: str) -> None:
        self.post("/file/complete", {"contentHash": sha256, "contentHashAlgorithm": "SHA256", "fileId": file_id, "uploadId": upload_id})

    def download(self, file_id: str) -> dict[str, Any]:
        data = self.post("/file/getDownloadUrl", {"fileId": file_id})["data"]
        url = data.get("cdnUrl") if data.get("cdnUrl") and data.get("cdnSwitch", True) else data.get("url")
        if not url:
            raise CMCloudError("CMCloud did not return a download URL")
        return {"url": url, "expires_at": _download_expiry(url), "host": urlparse(url).hostname}

    def trash(self, file_id: str) -> None:
        self.post("/recyclebin/batchTrash", {"fileIds": [file_id]})


class CMCloudBroker:
    """Capability broker. Persists provider handles, never signed URLs."""

    def __init__(self, provider: CMCloudProvider, data_dir: Path):
        self.provider = provider
        self.sessions = data_dir / "cmcloud" / "sessions"
        self.sessions.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        if len(session_id) != 32 or any(c not in "0123456789abcdef" for c in session_id):
            raise CMCloudError("invalid CMCloud session id")
        return self.sessions / f"{session_id}.json"

    def _load(self, session_id: str) -> dict[str, Any]:
        try:
            return json.loads(self._path(session_id).read_text())
        except FileNotFoundError as exc:
            raise CMCloudError("CMCloud session not found") from exc

    def _save(self, item: dict[str, Any]) -> None:
        _atomic_text(self._path(item["session_id"]), json.dumps(item, separators=(",", ":")), 0o600)

    def create_upload(self, *, size: int, sha256: str, parent_id: str = "/", name: str | None = None, encryption: str = "none", opaque_metadata: str | None = None, part_size: int | None = None) -> dict[str, Any]:
        if size <= 0 or len(sha256) != 64 or any(c not in "0123456789abcdefABCDEF" for c in sha256):
            raise CMCloudError("size and SHA-256 are required")
        if encryption not in {"none", "e2ee"}:
            raise CMCloudError("unsupported encryption mode")
        provider_name = f"p20-{uuid.uuid4().hex}.blob" if encryption == "e2ee" else (name or f"p20-{uuid.uuid4().hex}.bin")
        created = self.provider.create_upload(parent_id=parent_id, name=provider_name, size=size, sha256=sha256.lower(), part_size=part_size)
        session = {"session_id": uuid.uuid4().hex, "provider": "cmcloud", "file_id": created["file_id"], "upload_id": created["upload_id"], "provider_name": provider_name, "size": size, "sha256": sha256.lower(), "part_size": created["part_size"], "part_count": created["part_count"], "encryption": encryption, "opaque_metadata": opaque_metadata, "state": "uploading", "created_at": datetime.now(timezone.utc).isoformat()}
        if created["exist"] or created["rapid_upload"] and not created["parts"]:
            session["state"] = "completed"
        self._save(session)
        return {**session, "parts": created["parts"], "exist": created["exist"], "rapid_upload": created["rapid_upload"]}

    def issue_parts(self, session_id: str, part_numbers: list[int]) -> dict[str, Any]:
        session = self._load(session_id)
        if session["state"] not in {"uploading", "created"}:
            raise CMCloudError("CMCloud upload session is not active")
        parts = self.provider.get_upload_urls(file_id=session["file_id"], upload_id=session["upload_id"], size=session["size"], part_size=session["part_size"], part_numbers=part_numbers)
        return {"session_id": session_id, "parts": parts}

    def complete(self, session_id: str) -> dict[str, Any]:
        session = self._load(session_id)
        if session["state"] != "completed":
            self.provider.complete(file_id=session["file_id"], upload_id=session["upload_id"], sha256=session["sha256"])
            session["state"] = "completed"
            session["completed_at"] = datetime.now(timezone.utc).isoformat()
            self._save(session)
        return session

    def download(self, session_id: str) -> dict[str, Any]:
        session = self._load(session_id)
        cap = self.provider.download(session["file_id"])
        return {
            "session_id": session_id,
            **cap,
            "encryption": session.get("encryption", "none"),
            "opaque_metadata": session.get("opaque_metadata"),
            "size": session.get("size"),
            "sha256": session.get("sha256"),
        }

    def trash(self, session_id: str) -> dict[str, Any]:
        session = self._load(session_id)
        if session.get("state") != "trashed":
            self.provider.trash(session["file_id"])
            session["state"] = "trashed"
            session["trashed_at"] = datetime.now(timezone.utc).isoformat()
            self._save(session)
        return session
