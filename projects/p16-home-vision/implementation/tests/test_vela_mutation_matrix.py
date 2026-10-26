"""Exact, software-only canaries for mutations named by the frozen P16 soak catalog."""
from datetime import datetime, time, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import select
import pytest

from app.domain.auto_archive import closed_day_cutoff_utc, enqueue_closed_day_backlog
from app.domain.archive_pipeline import ArchiveValidationError, verify_part
from app.infra.media.ffprobe import FfprobeMediaProbe
from app.infra.nvr.hikvision import HikvisionIsapiClient
from app.models import ArchiveJob, CameraTrack, NvrDevice, ScanRun, SourceRecording
from app.domain.nvr_scan import scan_camera_range
from app.models import LocalAsset


def _seed_current_day_source(session, day, run_at):
    nvr = NvrDevice(id="s031-nvr", name="S031 synthetic", host="127.0.0.1", timezone="UTC")
    camera = CameraTrack(id="s031-camera", nvr_id=nvr.id, track_id="101",
                         camera_name="S031 synthetic")
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.flush()
    start = datetime.combine(day, time(1), tzinfo=timezone.utc)
    end = start + timedelta(minutes=3)
    scans = [
        ScanRun(id=f"s031-scan-{index}", nvr_id=nvr.id, track_id=camera.track_id,
                query_start=datetime.combine(day - timedelta(days=1), time.min,
                                             tzinfo=timezone.utc),
                query_end=datetime.combine(day + timedelta(days=1), time.min,
                                           tzinfo=timezone.utc),
                state="success", result_count=1, response_hash="stable-synthetic-hash",
                started_at=run_at - timedelta(minutes=3-index),
                completed_at=run_at - timedelta(minutes=2-index))
        for index in (1, 2)
    ]
    session.add_all(scans)
    session.flush()
    source = SourceRecording(id="s031-current-source", nvr_id=nvr.id, track_id=camera.track_id,
                             source_start=start, source_end=end, duration_ms=180000,
                             locator_fingerprint="synthetic-only", first_scan_id=scans[0].id,
                             last_scan_id=scans[1].id)
    session.add(source)
    session.flush()
    return nvr, source


def _run(session, now):
    return enqueue_closed_day_backlog(session, nvr_id="s031-nvr", now=now,
                                      enabled=True, timezone_verified=True)


def test_s031_current_day_enqueue_mutant_is_killed(session):
    """C1: the oracle kills a cutoff-removal mutant and accepts the real guard."""
    day = datetime.now(timezone.utc).date()
    now = datetime.combine(day, time(12), tzinfo=timezone.utc)
    expected_cutoff = closed_day_cutoff_utc(now=now, timezone_name="UTC")

    _, source = _seed_current_day_source(session, day, now)
    original_scalars = session.scalars

    def remove_closed_day_predicate(statement, *args, **kwargs):
        if (statement.column_descriptions
                and statement.column_descriptions[0].get("entity") is SourceRecording):
            # Mutation: preserve NVR/state filters and scan checks while
            # dropping only source_end <= cutoff from the real query.
            statement = (select(SourceRecording)
                         .where(SourceRecording.nvr_id == "s031-nvr",
                                SourceRecording.source_state == "available")
                         .order_by(SourceRecording.source_start.asc(),
                                   SourceRecording.track_id.asc()))
        return original_scalars(statement, *args, **kwargs)

    with patch.object(session, "scalars", side_effect=remove_closed_day_predicate):
        mutant_result = _run(session, now)
        session.flush()
        mutant_jobs = list(session.scalars(select(ArchiveJob)))
        # Independent UTC calendar oracle detects the mutant because the
        # current-day source ends after the true local-midnight cutoff.
        oracle_rejects = source.source_end >= expected_cutoff
        assert mutant_result.eligible_count == 1
        assert mutant_jobs and oracle_rejects

    session.rollback()
    _seed_current_day_source(session, day, now)
    actual_result = _run(session, now)
    session.flush()
    actual_jobs = list(session.scalars(select(ArchiveJob)))
    assert actual_result.cutoff_utc == expected_cutoff
    assert actual_result.eligible_count == 0
    assert actual_jobs == []


def test_s033_http_200_error_xml_and_invalid_video_are_rejected(session, tmp_path, monkeypatch):
    """Exercise both malformed-success boundaries without creating canonical rows."""
    day = datetime.now(timezone.utc).date() - timedelta(days=1)
    start = datetime.combine(day, time(1), tzinfo=timezone.utc)
    nvr = NvrDevice(id="s033-nvr", name="S033 synthetic", host="127.0.0.1", timezone="UTC")
    session.add(nvr)
    session.flush()
    camera = CameraTrack(id="s033-camera", nvr_id=nvr.id, track_id="101",
                         camera_name="S033 synthetic")
    session.add(camera)
    session.flush()

    error_xml = b'''<?xml version="1.0"?><ResponseStatus>
      <requestURL>/ISAPI/ContentMgmt/search</requestURL><statusCode>4</statusCode>
      <statusString>Invalid Operation</statusString><responseStatusStrg>FAIL</responseStatusStrg>
    </ResponseStatus>'''

    class Http200Error:
        status_code = 200
        content = error_xml

    class HttpClient:
        def post(self, *args, **kwargs):
            return Http200Error()

    client = HikvisionIsapiClient.__new__(HikvisionIsapiClient)
    client._base_url = "http://127.0.0.1/"
    client._client = HttpClient()
    client._download_method = "POST"
    result = scan_camera_range(
        session, nvr=nvr, camera=camera, query_start=start,
        query_end=start + timedelta(days=1), searcher=client,
    )
    session.flush()
    assert result.state == "failed"
    assert result.error_code == "NVR_SEARCH_REJECTED"
    assert list(session.scalars(select(SourceRecording))) == []

    invalid_part = tmp_path / "invalid-video.mp4"
    invalid_part.write_bytes(b"synthetic non-video payload\n" * 256)
    with pytest.raises(ArchiveValidationError) as rejected:
        verify_part(invalid_part, expected_duration_ms=180_000,
                    probe=FfprobeMediaProbe())
    assert rejected.value.code == "MEDIA_PROBE_FAILED"
    assert list(session.scalars(select(LocalAsset))) == []
