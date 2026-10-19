from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Protocol
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.domain.reconciliation import ReconciliationResult, reconcile_segments
from app.infra.nvr.hikvision import DiscoveredSegment, NvrProtocolError, build_search_request_xml
from app.models import CameraTrack, NvrDevice, ScanRun
from app.config import get_settings


class HistorySearcher(Protocol):
    def search_history(self, request_xml: bytes | str) -> list[DiscoveredSegment]: ...


@dataclass(frozen=True)
class ScanResult:
    scan_id: str
    state: str
    discovered_count: int
    error_code: str | None = None


def coalesce_segments(segments: Iterable[DiscoveredSegment]) -> list[DiscoveredSegment]:
    """Keep the longest rolling result for each camera and source start."""
    longest: dict[tuple[str, datetime], DiscoveredSegment] = {}
    for segment in segments:
        key = (segment.track_id, segment.source_start)
        current = longest.get(key)
        if current is None or segment.source_end > current.source_end:
            longest[key] = segment
    return list(longest.values())


def _response_hash(segments: list[DiscoveredSegment]) -> str:
    digest = hashlib.sha256()
    for segment in segments:
        digest.update(
            f"{segment.track_id}|{segment.source_start.isoformat()}|{segment.source_end.isoformat()}|{segment.locator_fingerprint}\n".encode()
        )
    return digest.hexdigest()


def scan_camera_range(
    session: Session,
    *,
    nvr: NvrDevice,
    camera: CameraTrack,
    query_start: datetime,
    query_end: datetime,
    searcher: HistorySearcher,
    scan_id: str | None = None,
    scan_min_date: date | None = None,
) -> ScanResult:
    """Run one bounded camera search and persist redacted scan coverage."""
    if camera.nvr_id != nvr.id:
        raise ValueError("NVR_CAMERA_MISMATCH")
    query_start = query_start.astimezone(timezone.utc)
    query_end = query_end.astimezone(timezone.utc)
    scan_min_date = get_settings().nvr_scan_min_date if scan_min_date is None else scan_min_date
    if scan_min_date is not None:
        try:
            business_zone = ZoneInfo(nvr.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("NVR_TIMEZONE_INVALID") from exc
        floor = datetime.combine(
            scan_min_date, datetime.min.time(), tzinfo=business_zone,
        ).astimezone(timezone.utc)
        if query_end <= floor:
            raise ValueError("NVR_SCAN_BEFORE_FLOOR")
        query_start = max(query_start, floor)
    scan = ScanRun(
        id=scan_id or f"scan-{uuid.uuid4().hex}",
        nvr_id=nvr.id,
        track_id=camera.track_id,
        query_start=query_start,
        query_end=query_end,
        state="running",
        result_count=0,
    )
    session.add(scan)
    session.flush()
    try:
        segments = coalesce_segments(searcher.search_history(
            build_search_request_xml(track_id=camera.track_id, query_start=query_start, query_end=query_end)
        ))
        result: ReconciliationResult = reconcile_segments(session, nvr_id=nvr.id, segments=segments, scan_id=scan.id)
        scan.state = "success"
        scan.result_count = result.discovered_count
        scan.response_hash = _response_hash(segments)
        scan.completed_at = datetime.now(timezone.utc)
        nvr.last_seen_at = scan.completed_at
        session.flush()
        return ScanResult(scan.id, scan.state, result.discovered_count)
    except NvrProtocolError as exc:
        scan.state = "failed"
        scan.error_code = exc.code
        scan.completed_at = datetime.now(timezone.utc)
        session.flush()
        return ScanResult(scan.id, scan.state, 0, exc.code)
