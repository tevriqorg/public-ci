from __future__ import annotations

import hashlib
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(element: ET.Element, name: str) -> str | None:
    for child in element.iter():
        if _local_name(child.tag) == name and child.text:
            return child.text.strip()
    return None


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class MockRecording:
    track_id: str
    start: datetime
    end: datetime
    payload: bytes
    recording_type: str = "normal"


class MockHikvisionNvr:
    """Loopback-only, deterministic ISAPI subset for protocol-level acceptance."""

    username = "fixture-user"
    password = "fixture-password"
    realm = "P16 Mock NVR"
    nonce = "p16-mock-nonce"

    def __init__(
        self,
        recordings: list[MockRecording],
        *,
        username: str = "fixture-user",
        password: str = "fixture-password",
        search_statuses: list[int] | None = None,
        download_statuses: list[int] | None = None,
        search_delays: list[float] | None = None,
        download_delays: list[float] | None = None,
        error_payload_once: bool = False,
        search_error_body_once: bytes | None = None,
        challenge_nonce: str | None = None,
        truncate_download_bytes_once: int | None = None,
        corrupt_media_once: bool = False,
    ) -> None:
        self.recordings = recordings
        self.username = username
        self.password = password
        self.search_statuses = list(search_statuses or [])
        self.download_statuses = list(download_statuses or [])
        self.search_delays = list(search_delays or [])
        self.download_delays = list(download_delays or [])
        self.error_payload_once = error_payload_once
        self.search_error_body_once = search_error_body_once
        self.challenge_nonce = self.nonce if challenge_nonce is None else challenge_nonce
        self.truncate_download_bytes_once = truncate_download_bytes_once
        self.corrupt_media_once = corrupt_media_once
        self.search_count = 0
        self.download_count = 0
        self.authorization_count = 0
        self.challenge_count = 0
        self.rejected_digest_count = 0
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                # Fixture server logs never contain headers, locators, or payloads.
                return

            def _send(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if body:
                    try:
                        self.wfile.write(body)
                    except OSError:
                        # A deliberate timeout injection may close the client
                        # before this fixture sends its delayed response.
                        pass
                self.close_connection = True

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                if not header.startswith("Digest "):
                    with owner._lock:
                        owner.challenge_count += 1
                    self.send_response(401)
                    self.send_header(
                        "WWW-Authenticate",
                        f'Digest realm="{owner.realm}", nonce="{owner.challenge_nonce}", algorithm=MD5, qop="auth"',
                    )
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    return False
                # Normalize the two capture groups and verify RFC 2617 qop=auth.
                values = {key: quoted or token for key, quoted, token in re.findall(
                    r'(\w+)=(?:"([^"]*)"|([^, ]+))', header
                )}
                method = self.command
                uri = self.path
                ha1 = hashlib.md5(f"{owner.username}:{owner.realm}:{owner.password}".encode()).hexdigest()
                ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
                qop = values.get("qop", "")
                expected = hashlib.md5(
                    f"{ha1}:{owner.nonce}:{values.get('nc', '')}:{values.get('cnonce', '')}:{qop}:{ha2}".encode()
                ).hexdigest()
                valid = (
                    values.get("username") == owner.username
                    and values.get("realm") == owner.realm
                    and values.get("nonce") == owner.nonce
                    and values.get("uri") == uri
                    and qop == "auth"
                    and values.get("response") == expected
                )
                if valid:
                    with owner._lock:
                        owner.authorization_count += 1
                else:
                    with owner._lock:
                        owner.rejected_digest_count += 1
                    self._send(403, b"", "text/plain")
                    return False
                return valid

            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
                self._send(405, b"", "text/plain")

            def do_PUT(self) -> None:  # noqa: N802 - stdlib callback name
                self._send(405, b"", "text/plain")

            def do_DELETE(self) -> None:  # noqa: N802 - stdlib callback name
                self._send(405, b"", "text/plain")

            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if not self._authorized():
                    return
                if self.path == "/ISAPI/ContentMgmt/search":
                    self._search(body)
                    return
                if self.path == "/ISAPI/ContentMgmt/download":
                    self._download(body)
                    return
                self._send(404, b"", "text/plain")

            def _search(self, body: bytes) -> None:
                try:
                    root = ET.fromstring(body)
                    track = _text(root, "trackID")
                    start = datetime.fromisoformat((_text(root, "startTime") or "").replace("Z", "+00:00"))
                    end = datetime.fromisoformat((_text(root, "endTime") or "").replace("Z", "+00:00"))
                except (ET.ParseError, ValueError):
                    self._send(400, b"", "text/plain")
                    return
                with owner._lock:
                    owner.search_count += 1
                    status = owner.search_statuses.pop(0) if owner.search_statuses else None
                    delay = owner.search_delays.pop(0) if owner.search_delays else 0
                if delay:
                    time.sleep(delay)
                if status is not None:
                    body = owner.search_error_body_once
                    owner.search_error_body_once = None
                    self._send(status, body or b"fixture search interruption", "text/plain")
                    return
                result = ET.Element("CMSearchResult", {"xmlns": "http://www.hikvision.com/ver20/XMLSchema"})
                matches = ET.SubElement(result, "matchList")
                for recording in owner.recordings:
                    if recording.track_id != track or recording.end <= start or recording.start >= end:
                        continue
                    item = ET.SubElement(matches, "searchMatchItem")
                    ET.SubElement(item, "trackID").text = recording.track_id
                    span = ET.SubElement(item, "timeSpan")
                    ET.SubElement(span, "startTime").text = _utc(recording.start)
                    ET.SubElement(span, "endTime").text = _utc(recording.end)
                    descriptor = ET.SubElement(item, "mediaSegmentDescriptor")
                    ET.SubElement(descriptor, "contentType").text = "video"
                    ET.SubElement(descriptor, "recordingType").text = recording.recording_type
                    key = int(recording.start.timestamp())
                    uri = f"rtsp://mock.invalid/Streaming/tracks/{recording.track_id}?starttime={key}&size={len(recording.payload)}"
                    ET.SubElement(descriptor, "playbackURI").text = uri
                self._send(200, ET.tostring(result, encoding="utf-8", xml_declaration=True), "application/xml")

            def _download(self, body: bytes) -> None:
                try:
                    root = ET.fromstring(body)
                    uri = _text(root, "playbackURI") or ""
                    query = parse_qs(urlsplit(uri).query)
                    start_epoch = int(query["starttime"][0])
                except (ET.ParseError, KeyError, ValueError):
                    self._send(400, b"", "text/plain")
                    return
                with owner._lock:
                    owner.download_count += 1
                    status = owner.download_statuses.pop(0) if owner.download_statuses else None
                    delay = owner.download_delays.pop(0) if owner.download_delays else 0
                    corrupt = owner.error_payload_once
                    owner.error_payload_once = False
                    truncate_bytes = owner.truncate_download_bytes_once
                    owner.truncate_download_bytes_once = None
                    corrupt_media = owner.corrupt_media_once
                    owner.corrupt_media_once = False
                if delay:
                    time.sleep(delay)
                if status is not None:
                    self._send(status, b"bandwidth limited" if status == 453 else b"fixture error", "text/plain")
                    return
                if corrupt:
                    error_body = b"<?xml version='1.0'?><ResponseStatus><statusCode>4</statusCode></ResponseStatus>" + b" " * 2048
                    self._send(200, error_body, "application/xml")
                    return
                recording = next((x for x in owner.recordings if int(x.start.timestamp()) == start_epoch), None)
                if recording is None:
                    self._send(404, b"", "text/plain")
                    return
                if truncate_bytes is not None:
                    partial = recording.payload[:max(1, min(truncate_bytes, len(recording.payload) - 1))]
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Length", str(len(recording.payload)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(partial)
                    self.wfile.flush()
                    self.close_connection = True
                    return
                if corrupt_media:
                    corrupted = b"not-a-video" + recording.payload[len(b"not-a-video"):]
                    self._send(200, corrupted, "video/mp4")
                    return
                self._send(200, recording.payload, "video/mp4")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> "MockHikvisionNvr":
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
