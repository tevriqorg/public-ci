#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

RECEIVER_VERSION = "0.3.0"
TICKET_SCHEMA = "p20.receiver-ticket/v2"
TICKET_SCHEMAS = {"p20.receiver-ticket/v1", TICKET_SCHEMA}
QFILE_HOST = "qfile.qq.com"
QFILE_DOWNLOAD_HOST = "multimedia.qfile.qq.com"
QFILE_PAGE_LIMIT = 2 * 1024 * 1024
DOWNLOAD_API = (
    "https://qfile.qq.com/http2rpc/gotrpc/noauth/"
    "trpc.qqntv2.richmedia.InnerProxy/BatchDownload"
)
WEB_HMAC_KEY = b"9EB18BB9ED457684"
USER_AGENT = "Mozilla/5.0 (P20 Receiver/0.1; +https://github.com/tevriqorg/P20-QQ-Runtime)"
CHUNK_SIZE = 1024 * 1024
QQ_RESOLVE_RETRY_DELAYS = (1, 2, 3, 4)


class ReceiverError(RuntimeError):
    pass


def _safe_http_url(value: str, *, host: str | None = None) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ReceiverError("expected an HTTP/HTTPS URL")
    if parsed.username or parsed.password:
        raise ReceiverError("credentials in URLs are not supported")
    if host and (parsed.scheme != "https" or parsed.hostname != host or parsed.port not in (None, 443)):
        raise ReceiverError(f"expected an official {host} HTTPS URL")
    return value


def _read_bounded(response, limit: int) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise ReceiverError("response is larger than the allowed limit")
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ReceiverError("response is larger than the allowed limit")
    return data


def _load_ticket(source: str) -> dict[str, Any]:
    if source == "-":
        raw = sys.stdin.buffer.read(QFILE_PAGE_LIMIT)
    elif source.startswith(("http://", "https://")):
        _safe_http_url(source)
        try:
            with urlopen(Request(source, headers={"User-Agent": USER_AGENT}), timeout=20) as response:
                raw = _read_bounded(response, QFILE_PAGE_LIMIT)
        except (HTTPError, URLError, TimeoutError) as exc:
            raise ReceiverError("unable to fetch receiver ticket") from exc
    else:
        raw = Path(source).read_bytes()
    try:
        ticket = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiverError("receiver ticket is not valid JSON") from exc
    if not isinstance(ticket, dict) or ticket.get("schema") not in TICKET_SCHEMAS:
        raise ReceiverError(f"receiver ticket schema must be one of {sorted(TICKET_SCHEMAS)}")
    return ticket


def _resolve_ref(root: list[Any], value: Any) -> Any:
    seen: set[int] = set()
    while isinstance(value, int) and 0 <= value < len(root) and value not in seen:
        seen.add(value)
        value = root[value]
    return value


def _nuxt_query_map(root: list[Any]) -> dict[str, Any]:
    try:
        top = _resolve_ref(root, root[0])
        state = _resolve_ref(root, top[1] if isinstance(top, list) else top)
        data = _resolve_ref(root, state["data"])
        query_map = _resolve_ref(root, data[1] if isinstance(data, list) else data)
    except (IndexError, KeyError, TypeError) as exc:
        raise ReceiverError("QQ share page metadata format is unsupported") from exc
    if not isinstance(query_map, dict):
        raise ReceiverError("QQ share page metadata is missing query data")
    return query_map


def _extract_qfile_items(html: str) -> list[dict[str, Any]]:
    match = re.search(
        r'<script[^>]+id="__NUXT_DATA__"[^>]*>(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    if not match:
        raise ReceiverError("QQ share page does not contain embedded metadata")
    try:
        root = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ReceiverError("QQ share page metadata is invalid") from exc
    if not isinstance(root, list):
        raise ReceiverError("QQ share page metadata root is invalid")
    query_map = _nuxt_query_map(root)
    query_key = next((key for key in query_map if "GetFileList_" in key), None)
    if not query_key:
        raise ReceiverError("QQ share page does not expose a file list")
    try:
        response = _resolve_ref(root, query_map[query_key])
        data = _resolve_ref(root, response["data"])
        group_refs = _resolve_ref(root, data["file_lists"])
    except (KeyError, TypeError) as exc:
        raise ReceiverError("QQ share page file list is incomplete") from exc

    items: list[dict[str, Any]] = []
    for group_ref in group_refs if isinstance(group_refs, list) else []:
        group = _resolve_ref(root, group_ref)
        if not isinstance(group, dict):
            continue
        file_refs = _resolve_ref(root, group.get("file_list"))
        for file_ref in file_refs if isinstance(file_refs, list) else []:
            item = _resolve_ref(root, file_ref)
            if not isinstance(item, dict):
                continue
            physical = _resolve_ref(root, item.get("physical"))
            if not isinstance(physical, dict):
                continue
            try:
                name = str(_resolve_ref(root, item.get("name")) or "")
                size = int(_resolve_ref(root, item.get("file_size")) or 0)
                physical_id = str(_resolve_ref(root, physical.get("id")) or "")
                sha1 = str(_resolve_ref(root, item.get("file_sha1")) or "")
                md5 = str(_resolve_ref(root, item.get("file_md5")) or "")
            except (TypeError, ValueError):
                continue
            if name and size >= 0 and physical_id:
                items.append({
                    "name": name,
                    "size": size,
                    "physical_id": physical_id,
                    "sha1": sha1,
                    "md5": md5,
                })
    if not items:
        raise ReceiverError("QQ share page contains no downloadable files")
    return items


def _fetch_qfile_items(share_link: str) -> list[dict[str, Any]]:
    _safe_http_url(share_link, host=QFILE_HOST)
    try:
        request = Request(share_link, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=20) as response:
            raw = _read_bounded(response, QFILE_PAGE_LIMIT)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise ReceiverError("unable to read QQ share page") from exc
    return _extract_qfile_items(raw.decode("utf-8", errors="replace"))


def _pick_qfile_item(items: list[dict[str, Any]], name: str, size: int) -> dict[str, Any]:
    same_size = [item for item in items if item["size"] == size]
    exact = [item for item in same_size if item["name"] == name]
    if len(exact) == 1:
        return exact[0]
    suffix = [item for item in same_size if item["name"].endswith(name)]
    if len(suffix) == 1:
        return suffix[0]
    if len(same_size) == 1:
        return same_size[0]
    raise ReceiverError("unable to identify the expected file on QQ share page")


def _download_request_body(physical_id: str) -> dict[str, Any]:
    return {
        "req_head": {"agent": 8},
        "download_info": [{
            "batch_id": physical_id,
            "scene": {"business_type": 4, "app_type": 22, "scene_type": 5},
            "index_node": {"file_uuid": physical_id},
            "url_type": 2,
            "download_scene": 0,
        }],
        "scene_type": 103,
    }


def _signed_download_url(item: dict[str, Any]) -> str:
    body = _download_request_body(item["physical_id"])
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    nonce = str(random.randint(1, 10000))
    timestamp = str(int(time.time()))
    signature = hmac.new(
        WEB_HMAC_KEY,
        raw + nonce.encode() + timestamp.encode(),
        hashlib.sha1,
    ).hexdigest()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Cookie": "uin=9000002;p_uin=9000002;",
        "User-Agent": USER_AGENT,
        "x-device-id": str(uuid.uuid4()),
        "x-oidb": '{"uint32_command":"0x9248", "uint32_service_type":"4"}',
        "x-qq-ar-nonce": nonce,
        "x-qq-ar-timestamp": timestamp,
        "x-qq-ar-signature": signature,
    }
    try:
        with urlopen(Request(DOWNLOAD_API, data=raw, headers=headers, method="POST"), timeout=20) as response:
            payload = json.loads(_read_bounded(response, 1024 * 1024))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ReceiverError("unable to obtain temporary QQ download URL") from exc
    error = payload.get("error") or {}
    error_code = str(error.get("code") or "0")
    if error_code == "170019016":
        raise ReceiverError("QQ requires a CAPTCHA for this download; try again later")
    if payload.get("retcode") not in {0, "0", None} or error_code not in {"0", ""}:
        raise ReceiverError(f"QQ download URL request failed with code {error_code}")
    data = payload.get("data") or {}
    responses = data.get("download_rsp") or data.get("downloadRsp") or []
    url = ""
    for entry in responses if isinstance(responses, list) else []:
        candidate = entry.get("url") if isinstance(entry, dict) else None
        if isinstance(candidate, str) and candidate:
            url = candidate
            break
    if not url:
        raise ReceiverError("QQ returned no downloadable URL")
    return _safe_http_url(url, host=QFILE_DOWNLOAD_HOST)


def _resolve_hub_download_url(
    artifact: dict[str, Any],
    progress=None,
    max_wait_seconds: int = 600,
) -> str | None:
    target = str(artifact.get("browser_resolve_url") or "")
    if not target:
        return None
    _safe_http_url(target)
    deadline = time.monotonic() + max(0, int(max_wait_seconds))
    while True:
        try:
            request = Request(target, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
            with urlopen(request, timeout=30) as response:
                payload = json.loads(_read_bounded(response, 1024 * 1024))
            url = str(payload.get("url") or "")
            if not url:
                raise ReceiverError("P20 receiver resolver returned no download URL")
            _safe_http_url(url, host=QFILE_DOWNLOAD_HOST)
            resolver = str(payload.get("resolver") or "P20")
            if progress:
                progress(f"P20 已通过 {resolver} 取得 QQ 临时下载地址。")
            return url
        except HTTPError as exc:
            if exc.code != 503:
                if progress:
                    progress(f"P20 Hub 直链解析不可用（HTTP {exc.code}），改用公开分享链路。")
                return None
            try:
                payload = json.loads(_read_bounded(exc, 1024 * 1024))
            except Exception:
                payload = {}
            detail = payload.get("detail") if isinstance(payload, dict) else {}
            if not isinstance(detail, dict):
                detail = {}
            message = str(detail.get("message") or "QQ 公开下载地址仍在同步。")
            waited = int(detail.get("waited_seconds") or 0)
            retry_after = int(detail.get("retry_after") or exc.headers.get("Retry-After") or 15)
            retry_after = max(1, min(retry_after, 60))
            if progress:
                progress(
                    f"{message} 已等待约 {waited} 秒；下一次将在 {retry_after} 秒后重试。"
                    "无需重新创建任务、重新下载源文件或重新上传 QQ；也可以中止后稍后重试。"
                )
            if max_wait_seconds <= 0 or time.monotonic() >= deadline:
                raise ReceiverError(
                    f"{message} 自动等待已达到 {max_wait_seconds} 秒；可稍后重新运行 p20-receive，任务无需重传。"
                ) from exc
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                raise ReceiverError(
                    f"{message} 自动等待已达到 {max_wait_seconds} 秒；可稍后重新运行 p20-receive，任务无需重传。"
                ) from exc
            time.sleep(min(retry_after, remaining))
        except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            if progress:
                progress("P20 Hub 暂时不可达，改用 QQ 公开分享链路。")
            return None


def _resolve_qfile_download_url(share_link: str, name: str, size: int, progress=None) -> str:
    last_error: ReceiverError | None = None
    for attempt in range(len(QQ_RESOLVE_RETRY_DELAYS) + 1):
        try:
            item = _pick_qfile_item(_fetch_qfile_items(share_link), name, size)
            url = _signed_download_url(item)
            if attempt and progress:
                progress("QQ 下载地址已同步，继续接收…")
            return url
        except ReceiverError as exc:
            if "CAPTCHA" in str(exc):
                raise
            last_error = exc
            if attempt == len(QQ_RESOLVE_RETRY_DELAYS):
                break
            delay = QQ_RESOLVE_RETRY_DELAYS[attempt]
            if progress:
                progress(
                    f"QQ Flash 已上传完成，但下载地址仍在同步。"
                    f"第 {attempt + 1}/{len(QQ_RESOLVE_RETRY_DELAYS)} 次重试将在 {delay} 秒后进行…"
                )
            time.sleep(delay)
    raise ReceiverError(
        "QQ Flash 已上传完成，但下载地址在约 10 秒内仍未同步；请稍后重新运行 p20-receive。"
    ) from last_error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _verified(path: Path, size: int, sha256: str) -> bool:
    return path.is_file() and path.stat().st_size == size and _sha256(path) == sha256


def _stream_download(url: str, part: Path, expected_size: int) -> None:
    offset = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": USER_AGENT}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    try:
        response = urlopen(Request(url, headers=headers), timeout=30)
    except HTTPError as exc:
        if exc.code == 416 and offset == expected_size:
            return
        raise ReceiverError("QQ CDN download request failed") from exc
    except (URLError, TimeoutError) as exc:
        raise ReceiverError("QQ CDN is temporarily unreachable") from exc

    with response:
        _safe_http_url(response.geturl(), host=QFILE_DOWNLOAD_HOST)
        status = getattr(response, "status", 200)
        mode = "wb"
        if offset and status == 206:
            content_range = response.headers.get("Content-Range", "")
            if not content_range.startswith(f"bytes {offset}-"):
                raise ReceiverError("QQ CDN returned an invalid resume range")
            mode = "ab"
        elif offset and status == 200:
            offset = 0
        elif offset:
            raise ReceiverError(f"QQ CDN returned unexpected status {status}")

        with part.open(mode) as handle:
            while chunk := response.read(CHUNK_SIZE):
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())


def _receive_artifact(
    artifact: dict[str, Any],
    output_dir: Path,
    *,
    force: bool = False,
    progress=None,
    receiver_wait_seconds: int = 600,
) -> dict[str, Any]:
    name = Path(str(artifact.get("name") or "")).name
    size = int(artifact.get("size") or 0)
    sha256 = str(artifact.get("sha256") or "").lower()
    source = artifact.get("source") or {}
    share_link = str(source.get("share_link") or "")
    if not name or size <= 0 or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ReceiverError("ticket artifact metadata is invalid")
    if source.get("kind") != "qq_flash_share":
        raise ReceiverError("unsupported receiver source kind")

    final = output_dir / name
    part = output_dir / f"{name}.part"
    if final.exists():
        if _verified(final, size, sha256):
            return {"name": name, "path": str(final), "size": size, "status": "existing"}
        if not force:
            raise ReceiverError(f"destination already exists with different content: {name}")
        final.unlink()
    if part.exists() and part.stat().st_size > size:
        part.unlink()
    if part.exists() and part.stat().st_size == size:
        if _verified(part, size, sha256):
            os.replace(part, final)
            return {"name": name, "path": str(final), "size": size, "status": "resumed"}
        part.unlink()

    def resolve_url() -> str:
        hub_url = _resolve_hub_download_url(
            artifact,
            progress=progress,
            max_wait_seconds=receiver_wait_seconds,
        )
        if hub_url:
            return hub_url
        return _resolve_qfile_download_url(share_link, name, size, progress=progress)

    signed_url = resolve_url()
    last_error: ReceiverError | None = None
    for attempt in range(3):
        try:
            _stream_download(signed_url, part, size)
        except ReceiverError as exc:
            last_error = exc
            if attempt < 2:
                signed_url = resolve_url()
            continue
        if not part.exists():
            last_error = ReceiverError("download did not create a partial file")
            continue
        current_size = part.stat().st_size
        if current_size < size:
            last_error = ReceiverError("download ended before the expected size")
            continue
        if current_size > size:
            part.unlink(missing_ok=True)
            raise ReceiverError("download exceeded the expected size")
        if _sha256(part) != sha256:
            raise ReceiverError(f"SHA256 verification failed for {name}")
        os.replace(part, final)
        return {"name": name, "path": str(final), "size": size, "status": "downloaded"}
    if last_error:
        raise last_error
    raise ReceiverError("download failed")



def _run_7z_password(argv: list[str], password: str) -> tuple[int, str]:
    if os.name != "posix":
        raise ReceiverError("secure automatic archive password handling currently requires macOS/Linux")
    if "\n" in password or "\r" in password:
        raise ReceiverError("archive password contains unsupported line breaks")
    import pty
    import select

    pid, master = pty.fork()
    if pid == 0:
        os.execvpe(argv[0], argv, os.environ.copy())

    output = bytearray()
    sent_prompts = 0
    prompt_re = re.compile(rb"(?:Enter|Reenter) password[^:\r\n]*:", re.I)
    status = None
    exit_seen_at = None
    try:
        while True:
            ready, _, _ = select.select([master], [], [], 0.2)
            eof = False
            if ready:
                try:
                    chunk = os.read(master, CHUNK_SIZE)
                except OSError:
                    chunk = b""
                    eof = True
                if chunk:
                    output.extend(chunk)
                    prompts = len(prompt_re.findall(output))
                    while sent_prompts < prompts:
                        os.write(master, (password + "\n").encode())
                        sent_prompts += 1
                else:
                    eof = True
            if status is None:
                waited, child_status = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    status = child_status
                    exit_seen_at = time.monotonic()
            if status is not None and (eof or (not ready and exit_seen_at is not None and time.monotonic() - exit_seen_at >= 0.2)):
                break
        return os.waitstatus_to_exitcode(status), output.decode(errors="replace")
    finally:
        try:
            os.close(master)
        except OSError:
            pass
        if status is None:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass


def _postprocess(ticket: dict[str, Any], received: list[dict[str, Any]], output_dir: Path, *,
                 force: bool = False, keep_archive: bool = False) -> dict[str, Any]:
    spec = ticket.get("postprocess") or {"kind": "none"}
    if spec.get("kind") in {None, "none"}:
        return {"kind": "none", "status": "not_required", "outputs": [item["path"] for item in received]}
    if spec.get("kind") != "archive":
        raise ReceiverError("unsupported receiver postprocess kind")
    exe = shutil.which("7zz") or shutil.which("7z")
    if not exe:
        raise ReceiverError("7z/7zz is required to decrypt/extract this transfer; archive remains downloaded")
    primary_name = str(spec.get("primary") or received[0]["name"])
    primary = next((Path(item["path"]) for item in received if item["name"] == primary_name), None)
    if primary is None:
        raise ReceiverError("receiver postprocess primary archive is missing")
    temp = output_dir / f".p20-extract-{str(ticket.get('transfer_id') or 'job')[:12]}"
    shutil.rmtree(temp, ignore_errors=True)
    temp.mkdir(parents=True, exist_ok=False)
    args = [exe, "x", "-y", f"-o{temp}", str(primary)]
    password = spec.get("password")
    if password:
        returncode, output = _run_7z_password(args, str(password))
    else:
        completed = subprocess.run(args, capture_output=True, text=True)
        returncode, output = completed.returncode, (completed.stdout or "") + (completed.stderr or "")
    if returncode != 0:
        shutil.rmtree(temp, ignore_errors=True)
        raise ReceiverError("archive decryption/extraction failed; verify the archive and password")
    outputs: list[str] = []
    for child in sorted(temp.iterdir(), key=lambda path: path.name):
        target = output_dir / child.name
        if target.exists():
            if not force:
                shutil.rmtree(temp, ignore_errors=True)
                raise ReceiverError(f"extracted destination already exists: {target.name}")
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.move(str(child), str(target))
        outputs.append(str(target))
    shutil.rmtree(temp, ignore_errors=True)
    if not keep_archive and spec.get("remove_archive_after_success", True):
        for item in received:
            Path(item["path"]).unlink(missing_ok=True)
    return {"kind": "archive", "status": "extracted", "outputs": outputs}


def _ack(ticket: dict[str, Any]) -> None:
    ack = ticket.get("ack") or {}
    if not ack.get("required"):
        return
    url = str(ack.get("url") or "")
    method = str(ack.get("method") or "POST").upper()
    _safe_http_url(url)
    if method != "POST":
        raise ReceiverError("unsupported ACK method")
    request = Request(
        url,
        data=b"{}",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            if not 200 <= getattr(response, "status", 200) < 300:
                raise ReceiverError("P20 ACK was rejected")
            _read_bounded(response, 1024 * 1024)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise ReceiverError("unable to ACK retrieval to P20") from exc


def receive(ticket: dict[str, Any], output_dir: Path, *, no_ack: bool = False, force: bool = False,
            keep_archive: bool = False, progress=None, receiver_wait_seconds: int = 600) -> dict[str, Any]:
    artifacts = ticket.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ReceiverError("receiver ticket contains no artifacts")
    output_dir.mkdir(parents=True, exist_ok=True)
    received = [
        _receive_artifact(
            item,
            output_dir,
            force=force,
            progress=progress,
            receiver_wait_seconds=receiver_wait_seconds,
        )
        for item in artifacts
        if isinstance(item, dict)
    ]
    if len(received) != len(artifacts):
        raise ReceiverError("receiver ticket contains invalid artifacts")
    postprocess = _postprocess(ticket, received, output_dir, force=force, keep_archive=keep_archive)
    if not no_ack:
        _ack(ticket)
    return {"ok": True, "transfer_id": ticket.get("transfer_id"), "artifacts": received,
            "postprocess": postprocess, "acked": not no_ack}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="p20-receive",
        description="Receive a P20 QQ Flash delivery without QQ login credentials.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {RECEIVER_VERSION}")
    parser.add_argument("--ticket", required=True, help="ticket URL, JSON file, or - for stdin")
    parser.add_argument("--output-dir", default=".", help="destination directory")
    parser.add_argument("--no-ack", action="store_true", help="verify files but do not acknowledge P20")
    parser.add_argument("--force", action="store_true", help="replace a conflicting destination file")
    parser.add_argument("--keep-archive", action="store_true", help="keep encrypted archive parts after successful extraction")
    parser.add_argument(
        "--receiver-wait-seconds",
        type=int,
        default=600,
        help="maximum time to wait for QQ public-share propagation when no runtime direct URL is available (default: 600)",
    )
    parser.add_argument("--json", action="store_true", dest="json_output", help="emit machine-readable result")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    progress = lambda message: print(f"p20-receive: {message}", file=sys.stderr, flush=True)
    try:
        ticket = _load_ticket(args.ticket)
        result = receive(
            ticket,
            Path(args.output_dir).expanduser().resolve(),
            no_ack=args.no_ack,
            force=args.force,
            keep_archive=args.keep_archive,
            progress=progress,
            receiver_wait_seconds=max(0, args.receiver_wait_seconds),
        )
    except (ReceiverError, OSError, ValueError) as exc:
        if args.json_output:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"p20-receive: {exc}", file=sys.stderr)
        return 1
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False))
    else:
        for item in result["artifacts"]:
            print(f"{item['status']}: {item['path']}")
        if result.get("postprocess", {}).get("status") == "extracted":
            for output in result["postprocess"].get("outputs", []):
                print(f"extracted: {output}")
        print("ACK: sent" if result["acked"] else "ACK: skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
