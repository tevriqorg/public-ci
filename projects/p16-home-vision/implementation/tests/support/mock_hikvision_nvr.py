from __future__ import annotations

import hashlib
import re
import threading
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
        download_statuses: list[int] | None = None,
        error_payload_once: bool = False,
    ) -> None:
        self.recordings = recordings
        self.download_statuses = list(download_statuses or [])
        self.error_payload_once = error_payload_once
        self.search_count = 0
        self.download_count = 0
        self.authorization_count = 0
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
                    self.wfile.write(body)
                self.close_connection = True

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                if not header.startswith("Digest "):
                    self.send_response(401)
                    self.send_header(
                        "WWW-Authenticate",
                        f'Digest realm="{owner.realm}", nonce="{owner.nonce}", algorithm=MD5, qop="auth"',
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
                    self._send(403, b"", "text/plain")
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
                    corrupt = owner.error_payload_once
                    owner.error_payload_once = False
                if status is not None:
                    self._send(status, b"bandwidth limited" if status == 453 else b"fixture error", "text/plain")
                    return
                if corrupt:
                    self._send(200, b"<?xml version='1.0'?><ResponseStatus><statusCode>4</statusCode></ResponseStatus>", "application/xml")
                    return
                recording = next((x for x in owner.recordings if int(x.start.timestamp()) == start_epoch), None)
                if recording is None:
                    self._send(404, b"", "text/plain")
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
