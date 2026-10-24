#!/usr/bin/env python3
import hashlib, hmac, ipaddress, json, os, pathlib, re, socket, sys, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIND = os.environ.get("QQXFER_BIND", "100.84.55.11")
PORT = int(os.environ.get("QQXFER_PORT", "17600"))
BASE = pathlib.Path(os.environ.get("QQXFER_BASE", "/home/ubuntu/qqxfer-api"))
SPOOL = pathlib.Path(os.environ.get(
    "QQXFER_SPOOL",
    "/home/ubuntu/qqxfer-llbot/llbot_config/qqxfer-spool",
))
API_TOKEN_FILE = pathlib.Path(os.environ.get(
    "QQXFER_API_TOKEN_FILE",
    str(BASE / "api.token"),
))
OB_TOKEN_FILE = pathlib.Path(os.environ.get(
    "QQXFER_OB_TOKEN_FILE",
    "/home/ubuntu/qqxfer-llbot/onebot.token",
))
API_TOKEN = API_TOKEN_FILE.read_text().strip()
OB_TOKEN = OB_TOKEN_FILE.read_text().strip()
OB_BASE = os.environ.get("QQXFER_OB_BASE", "http://127.0.0.1:16200").rstrip("/")
CHUNK = 1024 * 1024
MAX_JSON = 64 * 1024
MAX_EVENT_JSON = 1024 * 1024
FLASH_WAIT_TIMEOUT = int(os.environ.get("QQXFER_FLASH_WAIT_TIMEOUT", "3600"))
FLASH_VERIFY_TIMEOUT = int(os.environ.get("QQXFER_FLASH_VERIFY_TIMEOUT", "180"))
FLASH_VERIFY_INTERVAL = float(os.environ.get("QQXFER_FLASH_VERIFY_INTERVAL", "2"))
STATE_FILE = SPOOL / ".p20-flash-state.json"
STATE_LOCK = threading.RLock()
FLASH_STATUS = {}
FLASH_PATHS = {}
FLASH_EVENTS = {}
FLASH_IDEMPOTENCY = {}
FLASH_VERIFYING = set()


def _safe_spool_path(value):
    path = pathlib.Path(value).resolve()
    spool = SPOOL.resolve()
    if path.parent != spool:
        raise ValueError("refusing to delete path outside spool")
    return path


def _save_state():
    with STATE_LOCK:
        data = {"status": FLASH_STATUS, "paths": FLASH_PATHS, "idempotency": FLASH_IDEMPOTENCY}
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        os.replace(tmp, STATE_FILE)


def _load_state():
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text())
        FLASH_STATUS.update(data.get("status") or {})
        FLASH_PATHS.update(data.get("paths") or {})
        FLASH_IDEMPOTENCY.update(data.get("idempotency") or {})
        normalized = False
        uploaded_ids = []
        for file_set_id, st in FLASH_STATUS.items():
            if st.get("spool_cleaned") and st.get("sub_type") not in {"uploaded", "cancelled"}:
                st["sub_type"] = "uploaded"
                normalized = True
            FLASH_EVENTS.setdefault(file_set_id, threading.Event())
            if st.get("sub_type") in {"uploaded", "cancelled"}:
                uploaded_ids.append(file_set_id)
        if normalized:
            _save_state()
        for file_set_id in uploaded_ids:
            _cleanup_spool(file_set_id)
            FLASH_EVENTS[file_set_id].set()
    except Exception as e:
        sys.stderr.write(f"qqxfer-api: failed to load flash state: {e}\n")


def _cleanup_spool(file_set_id):
    with STATE_LOCK:
        raw = FLASH_PATHS.get(file_set_id)
    if not raw:
        return False
    path = _safe_spool_path(raw)
    path.unlink(missing_ok=True)
    with STATE_LOCK:
        FLASH_PATHS.pop(file_set_id, None)
        if file_set_id in FLASH_STATUS:
            FLASH_STATUS[file_set_id]["spool_cleaned"] = True
            FLASH_STATUS[file_set_id]["updated_at"] = int(time.time())
        _save_state()
    return True


def _update_flash_event(payload):
    file_set_id = str(payload.get("file_set_id") or "")
    if not file_set_id:
        return
    now = int(time.time())
    start_verifier = False
    with STATE_LOCK:
        current = dict(FLASH_STATUS.get(file_set_id) or {})
        incoming_sub_type = payload.get("sub_type")
        current_sub_type = current.get("sub_type")
        if current_sub_type == "cancelled":
            effective_sub_type = "cancelled"
        elif current_sub_type == "uploaded":
            effective_sub_type = "uploaded"
        elif incoming_sub_type == "uploaded":
            # The OneBot upload-complete notice only proves the local QQ client
            # finished its transfer. Do not publish success until QQ's own
            # Flash file list contains the expected remote payload.
            effective_sub_type = "verifying"
            if file_set_id not in FLASH_VERIFYING:
                FLASH_VERIFYING.add(file_set_id)
                start_verifier = True
        elif current_sub_type == "verifying":
            effective_sub_type = "verifying"
        else:
            effective_sub_type = incoming_sub_type
        current.update({
            "file_set_id": file_set_id,
            "title": payload.get("title") or current.get("title"),
            "share_link": payload.get("share_link") or current.get("share_link"),
            "sub_type": effective_sub_type,
            "uploaded_size": payload.get("uploaded_size") if payload.get("uploaded_size") is not None else current.get("uploaded_size"),
            "total_size": payload.get("total_size") if payload.get("total_size") is not None else current.get("total_size"),
            "speed": payload.get("speed") if payload.get("speed") is not None else current.get("speed"),
            "remain_seconds": payload.get("remain_seconds") if payload.get("remain_seconds") is not None else current.get("remain_seconds"),
            "updated_at": now,
        })
        FLASH_STATUS[file_set_id] = current
        FLASH_EVENTS.setdefault(file_set_id, threading.Event())
        _save_state()
    if start_verifier:
        threading.Thread(target=_verify_uploaded, args=(file_set_id,), daemon=True).start()


def _mark_cancelled(file_set_id):
    cleaned = _cleanup_spool(file_set_id)
    with STATE_LOCK:
        current = dict(FLASH_STATUS.get(file_set_id) or {})
        current.update({
            "file_set_id": file_set_id,
            "sub_type": "cancelled",
            "speed": 0,
            "remain_seconds": 0,
            "spool_cleaned": bool(current.get("spool_cleaned") or cleaned),
            "updated_at": int(time.time()),
        })
        FLASH_STATUS[file_set_id] = current
        event = FLASH_EVENTS.setdefault(file_set_id, threading.Event())
        _save_state()
        event.set()
        return dict(current)


def _register_flash(file_set_id, path, data):
    with STATE_LOCK:
        FLASH_PATHS[file_set_id] = str(_safe_spool_path(path))
        current = dict(FLASH_STATUS.get(file_set_id) or {})
        current.update({
            "file_set_id": file_set_id,
            "title": data.get("title"),
            "share_link": data.get("share_link"),
            "sub_type": current.get("sub_type") or "submitted",
            "spool_cleaned": False,
            "updated_at": int(time.time()),
        })
        FLASH_STATUS[file_set_id] = current
        event = FLASH_EVENTS.setdefault(file_set_id, threading.Event())
        already_uploaded = current.get("sub_type") == "uploaded"
        _save_state()
    if already_uploaded:
        _cleanup_spool(file_set_id)
    return event


def _wait_flash_uploaded(file_set_id, event):
    if not event.wait(FLASH_WAIT_TIMEOUT):
        raise TimeoutError(f"flash upload completion event timeout: {file_set_id}")
    with STATE_LOCK:
        status = dict(FLASH_STATUS.get(file_set_id) or {})
    if status.get("sub_type") == "failed":
        raise RuntimeError(status.get("verify_error") or f"QQ Flash remote verification failed: {file_set_id}")
    return status



def safe_name(s):
    s = pathlib.Path(s or "file.bin").name
    return "".join(c if c.isalnum() or c in "._- " else "_" for c in s)[:180] or "file.bin"

def llbot(path, payload, timeout=180):
    req = urllib.request.Request(
        OB_BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type":"application/json","Authorization":"Bearer "+OB_TOKEN},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode())
    if body.get("status") != "ok" or body.get("retcode") != 0:
        raise RuntimeError(body.get("message") or body.get("wording") or str(body))
    return body.get("data")

def validate_public_url(url):
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http","https") or not u.hostname:
        raise ValueError("only public http/https URLs are allowed")
    for info in socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme=="https" else 80), type=socket.SOCK_STREAM):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise ValueError("private/reserved destination is not allowed")

class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

opener = urllib.request.build_opener(SafeRedirect)

def submit_flash(dst, title, wait=True):
    remote = "/app/llbot/data/qqxfer-spool/" + dst.name
    data = llbot("/upload_flash_file", {"title": title, "paths": [remote]}, timeout=3600)
    file_set_id = str((data or {}).get("file_set_id") or "")
    if not file_set_id:
        raise RuntimeError("LLBot upload_flash_file returned no file_set_id")
    event = _register_flash(file_set_id, dst, {**(data or {}), "title": title})
    if wait:
        status = _wait_flash_uploaded(file_set_id, event)
    else:
        with STATE_LOCK:
            status = dict(FLASH_STATUS.get(file_set_id) or {})
    return data, status


def _flash_file_list(file_set_id):
    # LLBot 7.12.15 exposes the native Flash service internally but does not
    # expose a dedicated get_flash_file_url OneBot action. Keep the generic
    # bridge private to qqxfer-api and return only the selected download URL.
    groups = llbot("/llonebot_debug", {
        "apiClass": "ntFileApi",
        "method": "getFlashFileList",
        "args": [file_set_id],
    }, timeout=30)
    files = []
    for group in groups or []:
        for item in (group or {}).get("fileList") or []:
            files.append(item)
    if not files:
        raise RuntimeError("QQ Flash file list is not ready")
    return files




def _resolve_ref(root, value):
    seen = set()
    while isinstance(value, int) and 0 <= value < len(root) and value not in seen:
        seen.add(value)
        value = root[value]
    return value


def _extract_public_flash_sizes(html):
    match = re.search(
        r'<script[^>]+id="__NUXT_DATA__"[^>]*>(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    if not match:
        raise RuntimeError("QQ public share page has no embedded metadata")
    try:
        root = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError("QQ public share metadata is invalid") from exc
    if not isinstance(root, list):
        raise RuntimeError("QQ public share metadata root is invalid")
    try:
        top = _resolve_ref(root, root[0])
        state = _resolve_ref(root, top[1] if isinstance(top, list) else top)
        data = _resolve_ref(root, state["data"])
        query_map = _resolve_ref(root, data[1] if isinstance(data, list) else data)
        query_key = next(key for key in query_map if "GetFileList_" in key)
        response = _resolve_ref(root, query_map[query_key])
        response_data = _resolve_ref(root, response["data"])
        group_refs = _resolve_ref(root, response_data["file_lists"])
    except (IndexError, KeyError, StopIteration, TypeError) as exc:
        raise RuntimeError("QQ public share file list metadata is incomplete") from exc
    sizes = []
    for group_ref in group_refs if isinstance(group_refs, list) else []:
        group = _resolve_ref(root, group_ref)
        if not isinstance(group, dict):
            continue
        file_refs = _resolve_ref(root, group.get("file_list"))
        for file_ref in file_refs if isinstance(file_refs, list) else []:
            item = _resolve_ref(root, file_ref)
            if not isinstance(item, dict):
                continue
            try:
                size = int(_resolve_ref(root, item.get("file_size")) or 0)
            except (TypeError, ValueError):
                continue
            if size > 0:
                sizes.append(size)
    return sizes


def _public_flash_file_sizes(share_link):
    parsed = urllib.parse.urlparse(str(share_link or ""))
    if parsed.scheme != "https" or parsed.hostname != "qfile.qq.com" or parsed.port not in (None, 443):
        raise RuntimeError("QQ returned an unexpected public share URL")
    request = urllib.request.Request(share_link, headers={"User-Agent": "Mozilla/5.0 (P20 qqxfer verify)"})
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise RuntimeError("QQ public share page is too large")
    return _extract_public_flash_sizes(raw.decode("utf-8", errors="replace"))


def _verify_uploaded(file_set_id):
    deadline = time.monotonic() + max(1, FLASH_VERIFY_TIMEOUT)
    last_error = "QQ public share file list is not ready"
    public_sizes = []
    try:
        while True:
            with STATE_LOCK:
                status = dict(FLASH_STATUS.get(file_set_id) or {})
                raw_path = FLASH_PATHS.get(file_set_id)
            if status.get("sub_type") == "cancelled":
                return
            expected_size = 0
            if raw_path:
                try:
                    expected_size = _safe_spool_path(raw_path).stat().st_size
                except OSError:
                    pass
            if not expected_size:
                try:
                    expected_size = int(status.get("total_size") or 0)
                except (TypeError, ValueError):
                    expected_size = 0
            try:
                public_sizes = _public_flash_file_sizes(status.get("share_link"))
                if not public_sizes:
                    raise RuntimeError("QQ public share file list is empty")
                if expected_size and expected_size not in public_sizes:
                    raise RuntimeError("QQ public share file size does not match local payload")
                break
            except Exception as exc:
                last_error = str(exc)
                if time.monotonic() >= deadline:
                    with STATE_LOCK:
                        current = dict(FLASH_STATUS.get(file_set_id) or {})
                        if current.get("sub_type") != "cancelled":
                            current.update({
                                "sub_type": "failed",
                                "remote_verified": False,
                                "remote_verify_source": "qfile_public",
                                "verify_error": last_error,
                                "updated_at": int(time.time()),
                            })
                            FLASH_STATUS[file_set_id] = current
                            _save_state()
                            FLASH_EVENTS.setdefault(file_set_id, threading.Event()).set()
                    return
                time.sleep(max(0.1, FLASH_VERIFY_INTERVAL))
        with STATE_LOCK:
            current = dict(FLASH_STATUS.get(file_set_id) or {})
            if current.get("sub_type") == "cancelled":
                return
            current.update({
                "sub_type": "uploaded",
                "remote_verified": True,
                "remote_verify_source": "qfile_public",
                "remote_file_count": len(public_sizes),
                "verify_error": None,
                "updated_at": int(time.time()),
            })
            FLASH_STATUS[file_set_id] = current
            _save_state()
        _cleanup_spool(file_set_id)
        FLASH_EVENTS.setdefault(file_set_id, threading.Event()).set()
    finally:
        with STATE_LOCK:
            FLASH_VERIFYING.discard(file_set_id)


def _resume_verifiers():
    pending = []
    with STATE_LOCK:
        for file_set_id, status in FLASH_STATUS.items():
            event = FLASH_EVENTS.setdefault(file_set_id, threading.Event())
            if status.get("sub_type") == "verifying" and file_set_id not in FLASH_VERIFYING:
                FLASH_VERIFYING.add(file_set_id)
                pending.append(file_set_id)
            elif status.get("sub_type") in {"uploaded", "cancelled", "failed"}:
                event.set()
    for file_set_id in pending:
        threading.Thread(target=_verify_uploaded, args=(file_set_id,), daemon=True).start()

def _pick_flash_file(files, name="", size=0, file_index=0):
    if name:
        exact = [item for item in files if str(item.get("name") or "") == name]
        if size:
            exact = [item for item in exact if int(item.get("fileSize") or item.get("filePhysicalSize") or 0) == size]
        if len(exact) == 1:
            return exact[0]
        suffix = [item for item in files if str(item.get("name") or "").endswith(name)]
        if size:
            suffix = [item for item in suffix if int(item.get("fileSize") or item.get("filePhysicalSize") or 0) == size]
        if len(suffix) == 1:
            return suffix[0]
    if size:
        same_size = [item for item in files if int(item.get("fileSize") or item.get("filePhysicalSize") or 0) == size]
        if len(same_size) == 1:
            return same_size[0]
    if 0 <= file_index < len(files):
        return files[file_index]
    raise RuntimeError("unable to identify the expected QQ Flash file")


def _validate_flash_download_url(url):
    parsed = urllib.parse.urlparse(str(url or ""))
    if parsed.scheme != "https" or parsed.hostname != "multimedia.qfile.qq.com" or parsed.port not in (None, 443):
        raise RuntimeError("QQ returned an unexpected download URL")
    return url


def resolve_flash_download_url(file_set_id, name="", size=0, file_index=0):
    target = _pick_flash_file(_flash_file_list(file_set_id), name, size, file_index)
    result = llbot("/llonebot_debug", {
        "apiClass": "pmhq",
        "method": "invoke",
        "args": ["nodeIKernelFlashTransferService/startFileTransferUrl", [target]],
    }, timeout=30)
    if int((result or {}).get("ret") or 0) != 0:
        raise RuntimeError((result or {}).get("errMsg") or "QQ failed to create a download URL")
    url = _validate_flash_download_url((result or {}).get("url"))
    return {
        "file_set_id": file_set_id,
        "url": url,
        "expire_timestamp_seconds": str((result or {}).get("expireTimestampSeconds") or ""),
        "filename": str(target.get("name") or name),
        "size": int(target.get("fileSize") or target.get("filePhysicalSize") or size or 0),
    }


_load_state()
_resume_verifiers()

class H(BaseHTTPRequestHandler):
    server_version = "qqxfer-api/0.3.3"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def out(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def empty(self, code=204):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def auth(self):
        return self.headers.get("Authorization","") == "Bearer " + API_TOKEN

    def json_body(self):
        n = int(self.headers.get("Content-Length","0"))
        if n <= 0 or n > MAX_JSON:
            raise ValueError("invalid JSON body size")
        return json.loads(self.rfile.read(n).decode())

    def do_GET(self):
        if self.path == "/health":
            try:
                data = llbot("/get_login_info", {}, timeout=10)
                with STATE_LOCK:
                    pending = sum(1 for x in FLASH_STATUS.values() if x.get("sub_type") not in {"uploaded", "cancelled"})
                spool_files = sum(1 for x in SPOOL.iterdir() if x.is_file() and x != STATE_FILE and not x.name.startswith(".")) if SPOOL.exists() else 0
                return self.out(200, {
                    "ok": True, "service": "qqxfer-api", "qq": data,
                    "flash_pending": pending, "spool_files": spool_files,
                    "capabilities": {"direct_download_url": True},
                })
            except Exception as e:
                return self.out(503, {"ok":False,"error":str(e)})
        if not self.auth():
            return self.out(401, {"ok":False,"error":"unauthorized"})
        if self.path.startswith("/v1/status"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            file_set_id = q.get("file_set_id", [""])[0]
            with STATE_LOCK:
                status = dict(FLASH_STATUS.get(file_set_id) or {})
            if not status:
                return self.out(404, {"ok": False, "error": "unknown file_set_id"})
            return self.out(200, {"ok": True, **status})
        return self.out(404, {"ok":False,"error":"not found"})

    def do_POST(self):
        try:
            if self.path == "/v1/events/onebot":
                return self.onebot_event()
            if not self.auth():
                return self.out(401, {"ok":False,"error":"unauthorized"})
            if self.path.startswith("/v1/upload"):
                return self.upload()
            if self.path == "/v1/cancel":
                return self.cancel_flash()
            if self.path == "/v1/fetch":
                return self.fetch()
            if self.path == "/v1/info":
                p = self.json_body()
                data = llbot("/get_flash_file_info", p, timeout=30)
                return self.out(200, {"ok":True, **data})
            if self.path == "/v1/download-url":
                p = self.json_body()
                file_set_id = str(p.get("file_set_id") or "")
                if not file_set_id:
                    return self.out(400, {"ok": False, "error": "file_set_id is required"})
                data = resolve_flash_download_url(
                    file_set_id,
                    str(p.get("filename") or ""),
                    int(p.get("size") or 0),
                    int(p.get("file_index") or 0),
                )
                return self.out(200, {"ok": True, **data})
            return self.out(404, {"ok":False,"error":"not found"})
        except Exception as e:
            return self.out(500, {"ok":False,"error":str(e)})

    def onebot_event(self):
        n = int(self.headers.get("Content-Length", "0"))
        if n <= 0 or n > MAX_EVENT_JSON:
            return self.out(400, {"ok": False, "error": "invalid event body size"})
        raw = self.rfile.read(n)
        supplied = self.headers.get("X-Signature", "")
        expected = "sha1=" + hmac.new(OB_TOKEN.encode(), raw, hashlib.sha1).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            return self.out(401, {"ok": False, "error": "invalid event signature"})
        payload = json.loads(raw.decode())
        if payload.get("post_type") == "notice" and payload.get("notice_type") == "flash_file":
            _update_flash_event(payload)
        return self.empty(204)

    def cancel_flash(self):
        p = self.json_body()
        file_set_id = str(p.get("file_set_id") or "")
        if not file_set_id:
            return self.out(400, {"ok": False, "error": "file_set_id is required"})
        with STATE_LOCK:
            current = dict(FLASH_STATUS.get(file_set_id) or {})
        if current.get("sub_type") == "uploaded":
            return self.out(409, {"ok": False, "error": "flash upload already completed"})
        data = llbot("/cancel_flash_file", {"file_set_id": file_set_id}, timeout=30)
        status = _mark_cancelled(file_set_id)
        return self.out(200, {
            "ok": True,
            "file_set_id": file_set_id,
            "cancelled": True,
            "flash_status": status,
            "llbot": data,
        })

    def upload(self):
        n = self.headers.get("Content-Length")
        if not n:
            raise ValueError("Content-Length required")
        n = int(n)
        if n <= 0:
            raise ValueError("empty body")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = safe_name(self.headers.get("X-Filename") or q.get("filename",["file.bin"])[0])
        title = self.headers.get("X-Title") or q.get("title",[name])[0]
        wait = q.get("wait", ["1"])[0].lower() not in ("0", "false", "no")
        dst = SPOOL / (hashlib.sha256(os.urandom(32)).hexdigest()[:12] + "-" + name)
        h = hashlib.sha256()
        left = n
        try:
            with dst.open("wb") as f:
                while left:
                    b = self.rfile.read(min(CHUNK,left))
                    if not b:
                        raise IOError("request body ended early")
                    f.write(b); h.update(b); left -= len(b)
            idem = (self.headers.get("X-Idempotency-Key") or "").strip()[:300]
            if idem:
                digest = h.hexdigest()
                with STATE_LOCK:
                    existing = FLASH_IDEMPOTENCY.get(idem)
                    if isinstance(existing, dict):
                        existing_id = str(existing.get("file_set_id") or "")
                        existing_size = existing.get("size")
                        existing_sha256 = existing.get("sha256")
                    else:
                        existing_id = str(existing or "")
                        existing_size = existing_sha256 = None
                    existing_status = dict(FLASH_STATUS.get(existing_id) or {}) if existing_id else {}
                if existing_id and existing_status:
                    if existing_size not in (None, n) or existing_sha256 not in (None, digest):
                        dst.unlink(missing_ok=True)
                        return self.out(409, {
                            "ok": False, "error": "idempotency key already belongs to a different payload",
                            "file_set_id": existing_id,
                        })
                    dst.unlink(missing_ok=True)
                    complete = existing_status.get("sub_type") == "uploaded"
                    return self.out(200 if complete else 202, {
                        "ok": True, "filename": name, "size": n, "sha256": digest,
                        "file_set_id": existing_id, "upload_complete": complete,
                        "flash_status": existing_status, "idempotent_replay": True,
                    })
            data, status = submit_flash(dst, title, wait=wait)
            file_set_id = str((data or {}).get("file_set_id") or "")
            if idem and file_set_id:
                with STATE_LOCK:
                    FLASH_IDEMPOTENCY[idem] = {
                        "file_set_id": file_set_id, "size": n, "sha256": h.hexdigest(),
                    }
                    _save_state()
            complete = status.get("sub_type") == "uploaded"
            return self.out(200 if complete else 202, {
                "ok": True, "filename": name, "size": n, "sha256": h.hexdigest(),
                "upload_complete": complete, "flash_status": status, **data,
            })
        finally:
            pass

    def fetch(self):
        p = self.json_body()
        url = p.get("url","")
        validate_public_url(url)
        u = urllib.parse.urlparse(url)
        name = safe_name(p.get("filename") or pathlib.Path(urllib.parse.unquote(u.path)).name or "download.bin")
        title = p.get("title") or name
        dst = SPOOL / (hashlib.sha256(os.urandom(32)).hexdigest()[:12] + "-" + name)
        h = hashlib.sha256(); total = 0
        try:
            req = urllib.request.Request(url, headers={"User-Agent":"qqxfer-api/0.2"})
            with opener.open(req, timeout=60) as r, dst.open("wb") as f:
                while True:
                    b = r.read(CHUNK)
                    if not b: break
                    f.write(b); h.update(b); total += len(b)
            data, status = submit_flash(dst, title)
            return self.out(200, {
                "ok": True, "source_url": url, "filename": name, "size": total,
                "sha256": h.hexdigest(), "upload_complete": True,
                "flash_status": status, **data,
            })
        finally:
            pass

if __name__ == "__main__":
    SPOOL.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer((BIND,PORT), H).serve_forever()
