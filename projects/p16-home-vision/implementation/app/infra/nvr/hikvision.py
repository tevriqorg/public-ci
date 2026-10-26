from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from contextlib import contextmanager
from typing import Literal
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

import httpx


class NvrProtocolError(RuntimeError):
    """Redacted NVR protocol failure safe for logs/evidence."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DiscoveredSegment:
    track_id: str
    source_start: datetime
    source_end: datetime
    duration_ms: int
    locator_fingerprint: str
    playback_uri: str
    source_size_if_known: int | None = None
    content_type: str | None = None
    recording_type: str | None = None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first_text(element: ET.Element, name: str) -> str | None:
    for child in element.iter():
        if _local_name(child.tag) == name and child.text:
            value = child.text.strip()
            if value:
                return value
    return None


def parse_nvr_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise NvrProtocolError("NVR_TIMESTAMP_WITHOUT_TIMEZONE")
    return parsed.astimezone(timezone.utc)


def locator_fingerprint(playback_uri: str) -> str:
    """Return durable evidence without exposing the secret-bearing locator."""
    return hashlib.sha256(playback_uri.encode("utf-8")).hexdigest()


def playback_size_if_known(playback_uri: str) -> int | None:
    raw_size = parse_qs(urlsplit(playback_uri).query).get("size", [None])[0]
    try:
        size = int(raw_size) if raw_size is not None else 0
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


def parse_search_response(xml_payload: bytes | str) -> list[DiscoveredSegment]:
    try:
        root = ET.fromstring(xml_payload)
    except ET.ParseError as exc:
        raise NvrProtocolError("NVR_SEARCH_XML_INVALID") from exc

    # Some Hikvision firmware returns an XML error document with HTTP 200. An
    # empty parse result must not be recorded as a successful empty scan.
    response_status = next(
        (item for item in root.iter() if _local_name(item.tag) == "ResponseStatus"),
        None,
    )
    if response_status is not None:
        status_text = _first_text(response_status, "responseStatusStrg")
        status_code = _first_text(response_status, "statusCode")
        if ((status_text is not None and status_text.upper() not in {"OK", "SUCCESS"})
                or (status_code is not None and status_code not in {"1", "200"})):
            raise NvrProtocolError("NVR_SEARCH_REJECTED")

    segments: list[DiscoveredSegment] = []
    for item in root.iter():
        if _local_name(item.tag) not in {"searchMatchItem", "matchItem"}:
            continue
        track_id = _first_text(item, "trackID")
        start_raw = _first_text(item, "startTime")
        end_raw = _first_text(item, "endTime")
        playback_uri = _first_text(item, "playbackURI") or _first_text(item, "playbackUri")
        if not (track_id and start_raw and end_raw and playback_uri):
            continue
        start = parse_nvr_datetime(start_raw)
        end = parse_nvr_datetime(end_raw)
        if end <= start:
            raise NvrProtocolError("NVR_SEGMENT_TIME_INVALID")
        segments.append(
            DiscoveredSegment(
                track_id=track_id,
                source_start=start,
                source_end=end,
                duration_ms=round((end - start).total_seconds() * 1000),
                locator_fingerprint=locator_fingerprint(playback_uri),
                playback_uri=playback_uri,
                source_size_if_known=playback_size_if_known(playback_uri),
                content_type=_first_text(item, "contentType"),
                recording_type=_first_text(item, "recordingType"),
            )
        )
    return segments


def build_download_request_xml(playback_uri: str) -> bytes:
    # Hikvision ContentMgmt/download expects the path/query form returned by
    # the search result. Strip authority/userinfo so credentials never cross
    # the XML boundary and so this NVR returns the complete segment.
    parts = urlsplit(playback_uri)
    normalized_uri = urlunsplit(("", "", parts.path, parts.query, parts.fragment)) if parts.scheme or parts.netloc else playback_uri
    root = ET.Element("downloadRequest", {"version": "1.0", "xmlns": "http://www.isapi.org/ver20/XMLSchema"})
    child = ET.SubElement(root, "playbackURI")
    child.text = normalized_uri
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def build_search_request_xml(
    *,
    track_id: str,
    query_start: datetime,
    query_end: datetime,
    max_results: int = 1000,
) -> bytes:
    """Build a bounded, read-only Hikvision ContentMgmt search request."""
    if query_end <= query_start:
        raise NvrProtocolError("NVR_SEARCH_RANGE_INVALID")
    if max_results < 1 or max_results > 10000:
        raise NvrProtocolError("NVR_SEARCH_MAX_RESULTS_INVALID")
    root = ET.Element("CMSearchDescription", {"version": "1.0", "xmlns": "http://www.isapi.org/ver20/XMLSchema"})
    ET.SubElement(root, "searchID").text = str(uuid.uuid4())
    track_list = ET.SubElement(root, "trackList")
    ET.SubElement(track_list, "trackID").text = track_id
    time_span_list = ET.SubElement(root, "timeSpanList")
    time_span = ET.SubElement(time_span_list, "timeSpan")
    ET.SubElement(time_span, "startTime").text = query_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    ET.SubElement(time_span, "endTime").text = query_end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    ET.SubElement(root, "maxResults").text = str(max_results)
    ET.SubElement(root, "searchResultPostion").text = "0"
    metadata_list = ET.SubElement(root, "metadataList")
    ET.SubElement(metadata_list, "metadataDescriptor").text = "//recordType.meta.std-cgi.com"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


class HikvisionIsapiClient:
    """Minimal real ISAPI transport. Credentials are injected at runtime only."""

    SEARCH_PATH = "/ISAPI/ContentMgmt/search"

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        timeout_seconds: float = 30.0,
        download_method: Literal["GET", "POST"] = "GET",
    ) -> None:
        self._base_url = base_url.rstrip("/") + "/"
        self._download_method = download_method
        self._client = httpx.Client(
            auth=httpx.DigestAuth(username, password),
            timeout=timeout_seconds,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HikvisionIsapiClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def search_history(self, request_xml: bytes | str) -> list[DiscoveredSegment]:
        url = urljoin(self._base_url, self.SEARCH_PATH.lstrip("/"))
        try:
            response = self._client.post(
                url,
                content=request_xml,
                headers={"Content-Type": "application/xml"},
            )
        except httpx.HTTPError as exc:
            raise NvrProtocolError("NVR_SEARCH_TRANSPORT_FAILED") from exc
        if response.status_code == 453:
            raise NvrProtocolError("NVR_BANDWIDTH_LIMIT")
        if response.status_code >= 400:
            raise NvrProtocolError(f"NVR_SEARCH_HTTP_{response.status_code}")
        return parse_search_response(response.content)

    def download_to_part(
        self,
        playback_uri: str,
        part_path: Path,
        *,
        progress_callback: Callable[[int], None] | None = None,
    ) -> int:
        """Download one search-returned playback URI through ISAPI ContentMgmt/download."""
        endpoint = urljoin(self._base_url, "/ISAPI/ContentMgmt/download".lstrip("/"))
        body = build_download_request_xml(playback_uri)
        part_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._client.stream(
                self._download_method,
                endpoint,
                content=body,
                headers={"Content-Type": "application/xml"},
            ) as response:
                if response.status_code == 453:
                    raise NvrProtocolError("NVR_BANDWIDTH_LIMIT")
                if response.status_code >= 400:
                    raise NvrProtocolError(f"NVR_DOWNLOAD_HTTP_{response.status_code}")
                received = 0
                with part_path.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        if not chunk:
                            continue
                        handle.write(chunk)
                        received += len(chunk)
                        if progress_callback is not None:
                            progress_callback(received)
                    handle.flush()
                return received
        except NvrProtocolError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise NvrProtocolError("NVR_DOWNLOAD_FAILED") from exc

    @contextmanager
    def stream_download(self, playback_uri: str, *, max_bytes: int):
        """Yield a bounded playback stream; callers must not persist the video."""
        if max_bytes < 1:
            raise ValueError("NVR_STREAM_MAX_BYTES_INVALID")
        endpoint = urljoin(self._base_url, "/ISAPI/ContentMgmt/download".lstrip("/"))
        body = build_download_request_xml(playback_uri)
        try:
            with self._client.stream(
                self._download_method,
                endpoint,
                content=body,
                headers={"Content-Type": "application/xml"},
            ) as response:
                if response.status_code == 453:
                    raise NvrProtocolError("NVR_BANDWIDTH_LIMIT")
                if response.status_code >= 400:
                    raise NvrProtocolError(f"NVR_DOWNLOAD_HTTP_{response.status_code}")

                def bounded_chunks():
                    received = 0
                    for chunk in response.iter_bytes():
                        if not chunk:
                            continue
                        remaining = max_bytes - received
                        if remaining <= 0:
                            return
                        yield chunk[:remaining]
                        received += min(len(chunk), remaining)
                        if received >= max_bytes:
                            return

                yield bounded_chunks()
        except NvrProtocolError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise NvrProtocolError("NVR_DOWNLOAD_FAILED") from exc
