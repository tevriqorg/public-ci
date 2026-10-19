from datetime import date, datetime, timedelta, timezone

import pytest

from app.domain.reconciliation import scan_state_advances_expiry, should_mark_expired
from app.domain.nvr_scan import coalesce_segments, scan_camera_range
from app.infra.nvr.hikvision import (
    DiscoveredSegment,
    NvrProtocolError,
    build_download_request_xml,
    build_search_request_xml,
    locator_fingerprint,
    playback_size_if_known,
    parse_search_response,
)
from scripts.archive_worker import _find_exact_segment
from app.models import CameraTrack, NvrDevice, ScanRun, SourceRecording


SEARCH_XML = b'''<?xml version="1.0" encoding="UTF-8"?>
<CMSearchResult xmlns="http://www.hikvision.com/ver20/XMLSchema">
  <matchList>
    <searchMatchItem>
      <trackID>201</trackID>
      <timeSpan><startTime>2026-08-27T06:00:00Z</startTime><endTime>2026-08-27T07:12:00Z</endTime></timeSpan>
      <mediaSegmentDescriptor>
        <contentType>video</contentType>
        <playbackURI>rtsp://user:secret@example.invalid/Streaming/tracks/201?starttime=20260827T060000Z</playbackURI>
      </mediaSegmentDescriptor>
    </searchMatchItem>
  </matchList>
</CMSearchResult>'''


def test_parse_hikvision_search_and_hash_locator() -> None:
    segments = parse_search_response(SEARCH_XML)
    assert len(segments) == 1
    segment = segments[0]
    assert segment.track_id == "201"
    assert segment.duration_ms == 72 * 60 * 1000
    assert segment.source_size_if_known is None
    assert segment.locator_fingerprint == locator_fingerprint(segment.playback_uri)
    assert "secret" not in segment.locator_fingerprint


def test_only_successful_scans_advance_expiry() -> None:
    assert scan_state_advances_expiry("success")
    assert not scan_state_advances_expiry("partial")
    assert not scan_state_advances_expiry("failed")


def test_expiry_requires_two_successful_misses_and_old_segment() -> None:
    edge = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    recording = SourceRecording(
        id="src-x",
        nvr_id="n",
        track_id="201",
        source_start=edge - timedelta(days=3),
        source_end=edge - timedelta(days=3) + timedelta(hours=1),
        duration_ms=3600000,
    )
    assert not should_mark_expired(recording, successful_covering_misses=1, current_write_edge=edge)
    assert should_mark_expired(recording, successful_covering_misses=2, current_write_edge=edge)


def test_download_request_wraps_playback_uri_as_xml() -> None:
    uri = "rtsp://example.invalid/Streaming/tracks/201?start=1&end=2"
    body = build_download_request_xml(uri)
    assert b"<downloadRequest" in body
    assert b"<playbackURI>" in body
    assert b"&amp;end=2" in body
    assert uri.encode() not in body
    assert b"/Streaming/tracks/201?start=1&amp;end=2" in body
    assert b"example.invalid" not in body


def test_coalesce_segments_keeps_longest_rolling_result() -> None:
    start = datetime(2026, 9, 2, 21, 0, tzinfo=timezone.utc)
    short = DiscoveredSegment("201", start, start + timedelta(minutes=10), 600000, "short", "rtsp://short")
    long = DiscoveredSegment("201", start, start + timedelta(minutes=30), 1800000, "long", "rtsp://long")
    assert coalesce_segments([short, long, short]) == [long]


def test_playback_size_is_safe_metadata_only() -> None:
    assert playback_size_if_known("rtsp://example.invalid/stream?size=12345") == 12345
    assert playback_size_if_known("rtsp://example.invalid/stream?size=not-a-number") is None


def test_search_request_is_bounded_and_utc() -> None:
    body = build_search_request_xml(
        track_id="201",
        query_start=datetime(2026, 8, 27, 14, 0, tzinfo=timezone(timedelta(hours=8))),
        query_end=datetime(2026, 8, 27, 15, 0, tzinfo=timezone(timedelta(hours=8))),
        max_results=250,
    )
    assert b"<trackList><trackID>201</trackID></trackList>" in body
    assert b"<startTime>2026-08-27T06:00:00Z</startTime>" in body
    assert b"<endTime>2026-08-27T07:00:00Z</endTime>" in body
    assert b"<maxResults>250</maxResults>" in body


def test_worker_accepts_newer_end_of_same_rolling_nvr_segment() -> None:
    source = SourceRecording(
        id="source-rolling",
        nvr_id="nvr-rolling",
        track_id="101",
        source_start=datetime(2026, 9, 2, 21, 10, 15, tzinfo=timezone.utc),
        source_end=datetime(2026, 9, 2, 22, 32, 36, tzinfo=timezone.utc),
        duration_ms=4941000,
    )
    current = DiscoveredSegment(
        track_id="101",
        source_start=source.source_start,
        source_end=datetime(2026, 9, 2, 23, 0, tzinfo=timezone.utc),
        duration_ms=6585000,
        locator_fingerprint="rolling",
        playback_uri="rtsp://example.invalid/rolling",
    )

    class Searcher:
        def search_history(self, _request_xml):
            return [current]

    assert _find_exact_segment(Searcher(), source) == current


def test_scan_camera_range_persists_success_and_reconciles_sources(session) -> None:
    nvr = NvrDevice(id="scan-nvr", name="Scan NVR", host="fixture")
    camera = CameraTrack(id="scan-camera", nvr_id=nvr.id, track_id="201", camera_name="Camera 201")
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.flush()
    segment = parse_search_response(SEARCH_XML)[0]

    class Searcher:
        def __init__(self) -> None:
            self.request_xml: bytes | str | None = None

        def search_history(self, request_xml: bytes | str):
            self.request_xml = request_xml
            return [segment]

    searcher = Searcher()
    result = scan_camera_range(
        session,
        nvr=nvr,
        camera=camera,
        query_start=datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc),
        query_end=datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc),
        searcher=searcher,
        scan_id="scan-success",
    )
    session.commit()

    scan = session.get(ScanRun, "scan-success")
    assert result.state == "success"
    assert result.discovered_count == 1
    assert scan is not None
    assert scan.state == "success"
    assert scan.response_hash
    assert b"<trackID>201</trackID>" in searcher.request_xml
    assert session.query(SourceRecording).count() == 1
    assert nvr.last_seen_at is not None


def test_scan_camera_range_rejects_queries_before_domain_floor(session) -> None:
    nvr = NvrDevice(id="floor-nvr", name="Floor NVR", host="fixture")
    camera = CameraTrack(id="floor-camera", nvr_id=nvr.id, track_id="201", camera_name="Camera 201")
    session.add(nvr); session.flush(); session.add(camera); session.flush()
    with pytest.raises(ValueError, match="NVR_SCAN_BEFORE_FLOOR"):
        scan_camera_range(
            session,
            nvr=nvr,
            camera=camera,
            query_start=datetime(2026, 8, 27, tzinfo=timezone.utc),
            query_end=datetime(2026, 8, 28, tzinfo=timezone.utc),
            searcher=object(),
            scan_min_date=date(2026, 9, 1),
        )


def test_scan_camera_range_redacts_protocol_failure(session) -> None:
    nvr = NvrDevice(id="failed-nvr", name="Failed NVR", host="fixture")
    camera = CameraTrack(id="failed-camera", nvr_id=nvr.id, track_id="201", camera_name="Camera 201")
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.flush()

    class Searcher:
        def search_history(self, request_xml: bytes | str):
            raise NvrProtocolError("NVR_SEARCH_HTTP_401")

    result = scan_camera_range(
        session,
        nvr=nvr,
        camera=camera,
        query_start=datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc),
        query_end=datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc),
        searcher=Searcher(),
        scan_id="scan-failed",
    )
    session.commit()

    scan = session.get(ScanRun, "scan-failed")
    assert result == result.__class__("scan-failed", "failed", 0, "NVR_SEARCH_HTTP_401")
    assert scan is not None
    assert scan.state == "failed"
    assert scan.error_code == "NVR_SEARCH_HTTP_401"


def test_reconcile_duplicate_discovery_reuses_source_id(session) -> None:
    from app.domain.reconciliation import reconcile_segments
    from app.models import CameraTrack, NvrDevice

    nvr = NvrDevice(id="fixture-nvr", name="Fixture NVR", host="fixture")
    session.add(nvr)
    session.flush()
    session.add(CameraTrack(id="fixture-cam", nvr_id=nvr.id, track_id="201", camera_name="Fixture Cam"))
    session.flush()
    segment = parse_search_response(SEARCH_XML)[0]
    first = reconcile_segments(session, nvr_id=nvr.id, segments=[segment], scan_id=None)
    second = reconcile_segments(session, nvr_id=nvr.id, segments=[segment], scan_id=None)
    assert first.discovered_count == second.discovered_count == 1
    assert first.source_ids == second.source_ids


def test_scan_floor_uses_nvr_business_timezone(session) -> None:
    nvr = NvrDevice(id="floor-zone-nvr", name="Floor Zone NVR", host="fixture", timezone="Asia/Shanghai")
    camera = CameraTrack(id="floor-zone-camera", nvr_id=nvr.id, track_id="201", camera_name="Camera 201")
    session.add(nvr); session.flush(); session.add(camera); session.flush()

    class EmptySearcher:
        def search_history(self, _request):
            return []

    result = scan_camera_range(
        session, nvr=nvr, camera=camera,
        query_start=datetime(2026, 8, 31, 16, tzinfo=timezone.utc),
        query_end=datetime(2026, 9, 1, 16, tzinfo=timezone.utc),
        searcher=EmptySearcher(), scan_id="scan-local-floor",
        scan_min_date=date(2026, 9, 1),
    )
    scan = session.get(ScanRun, result.scan_id)
    assert scan is not None
    assert scan.query_start == datetime(2026, 8, 31, 16, tzinfo=timezone.utc)
