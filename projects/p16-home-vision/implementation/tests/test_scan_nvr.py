from datetime import date, datetime, timedelta, timezone

from scripts import scan_nvr
from scripts.scan_nvr import _has_successful_full_day_scan, _watch_range
from app.models import CameraTrack, NvrDevice, ScanRun


def test_default_watch_backfills_once_then_keeps_recent_days_fresh() -> None:
    today = date(2026, 9, 3)

    first_start, first_end = _watch_range(
        today=today,
        start_day=None,
        end_day=None,
    )
    next_start, next_end = _watch_range(
        today=today,
        start_day=None,
        end_day=None,
    )

    assert first_start == today - timedelta(days=60)
    assert first_end == today
    assert next_start == today - timedelta(days=60)
    assert next_end == today


def test_explicit_watch_range_remains_explicit() -> None:
    today = date(2026, 9, 3)
    start = date(2026, 8, 1)
    end = date(2026, 8, 7)

    assert _watch_range(today=today, start_day=start, end_day=end) == (start, end)


def test_first_watch_window_uses_configured_backfill_bound() -> None:
    today = date(2026, 9, 3)
    assert _watch_range(
        today=today,
        start_day=None,
        end_day=None,
        backfill_days=45,
    ) == (today - timedelta(days=45), today)


def test_gap_recovery_recognizes_only_successful_full_day_coverage(session) -> None:
    nvr = NvrDevice(id="nvr-gap", name="NVR", host="fixture")
    camera = CameraTrack(id="camera-gap", nvr_id=nvr.id, track_id="101", camera_name="Camera")
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.flush()
    session.add(
        ScanRun(
            id="scan-partial", nvr_id=nvr.id, track_id=camera.track_id,
            query_start=start, query_end=end, state="failed", result_count=0,
        )
    )
    session.flush()
    assert not _has_successful_full_day_scan(
        session, nvr_id=nvr.id, track_id=camera.track_id, day_start=start, day_end=end
    )
    session.query(ScanRun).filter_by(id="scan-partial").update({"state": "success"})
    session.flush()
    assert _has_successful_full_day_scan(
        session, nvr_id=nvr.id, track_id=camera.track_id, day_start=start, day_end=end
    )


def test_watch_range_respects_recent_backfill_and_minimum_date() -> None:
    today = date(2026, 9, 3)
    start, end = _watch_range(
        today=today,
        start_day=None,
        end_day=None,
        backfill_days=10,
        scan_min_date=date(2026, 8, 1),
    )
    assert start == today - timedelta(days=10)
    assert end == today


def test_cli_initializes_settings_before_watcher_loop(monkeypatch) -> None:
    settings = type("Settings", (), {
        "nvr_scan_initial_days": 10,
        "nvr_scan_min_date": None,
    })()
    seen = []
    monkeypatch.setattr(scan_nvr, "get_settings", lambda: settings)
    monkeypatch.setattr(scan_nvr, "_watch_today", lambda value: seen.append(value) or date(2026, 9, 3))
    monkeypatch.setattr(scan_nvr, "scan_once", lambda **kwargs: seen.append(kwargs))
    monkeypatch.setattr("sys.argv", ["scan_nvr.py"])
    assert scan_nvr.main() == 0
    assert seen[0] is settings
    assert seen[1]["start_day"] == date(2026, 8, 24)


def test_single_camera_failure_does_not_block_stable_peer(session, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from datetime import datetime, timedelta, timezone
    from pydantic import SecretStr
    from sqlalchemy import select
    from app.models import NvrDevice, CameraTrack, ArchiveJob
    from app.infra.nvr.hikvision import DiscoveredSegment, NvrProtocolError
    from scripts import scan_nvr

    today = datetime.now(timezone.utc).date()
    day = today - timedelta(days=1)
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=1)
    nvr = NvrDevice(id="nvr-camera-isolation", name="SYNTHETIC", host="127.0.0.1", timezone="UTC")
    session.add(nvr)
    session.flush()
    for track in ("101", "102"):
        session.add(CameraTrack(id="isolation-"+track, nvr_id=nvr.id, track_id=track, camera_name="Synthetic"))
    session.commit()
    settings = SimpleNamespace(nvr_base_url="http://127.0.0.1", nvr_username="fixture",
        nvr_password=SecretStr("fixture"), nvr_scan_min_date=None, nvr_tracks=None,
        nvr_id=nvr.id, archive_auto_enqueue=True, nvr_timezone_verified=True)
    class Searcher:
        def search_history(self, xml):
            import xml.etree.ElementTree as ET
            root = ET.fromstring(xml)
            track = next(e.text for e in root.iter() if e.tag.rsplit("}",1)[-1]=="trackID")
            if track == "102":
                raise NvrProtocolError("NVR_HTTP_ERROR")
            return [DiscoveredSegment("101", start, start+timedelta(minutes=3), 180000,
                                      "synthetic-fingerprint", "http://127.0.0.1/fixture")]
    monkeypatch.setattr(scan_nvr, "get_settings", lambda:settings)
    monkeypatch.setattr(scan_nvr, "SessionLocal", lambda:nullcontext(session))
    monkeypatch.setattr(scan_nvr, "HikvisionIsapiClient", lambda **kw:nullcontext(Searcher()))
    for _ in range(2):
        assert scan_nvr.scan_once(start_day=day, end_day=day) == 1
    jobs = list(session.scalars(select(ArchiveJob)))
    assert len(jobs) == 1
    assert jobs[0].state == "queued"
