from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.generate_unarchived_thumbnails import _next_noon, _select_sources
from app.infra.nvr.hikvision import HikvisionIsapiClient
from app.models import CameraTrack, LocalAsset, NvrDevice, ScanRun, SourceRecording, StorageVolume


def _seed_source(session, source_id: str, *, scan_state: str = "success", archived: bool = False):
    nvr = NvrDevice(id=f"nvr-{source_id}", name="NVR", host="fixture")
    camera = CameraTrack(id=f"camera-{source_id}", nvr_id=nvr.id, track_id="101", camera_name="Camera")
    scan = ScanRun(
        id=f"scan-{source_id}",
        nvr_id=nvr.id,
        track_id=camera.track_id,
        query_start=datetime(2026, 8, 1, tzinfo=timezone.utc),
        query_end=datetime(2026, 8, 2, tzinfo=timezone.utc),
        state=scan_state,
    )
    source = SourceRecording(
        id=source_id,
        nvr_id=nvr.id,
        track_id=camera.track_id,
        source_start=datetime(2026, 8, 1, 8, tzinfo=timezone.utc),
        source_end=datetime(2026, 8, 1, 8, 0, 10, tzinfo=timezone.utc),
        duration_ms=10000,
        source_state="available",
        last_confirmed_at=datetime(2026, 8, 1, 9, tzinfo=timezone.utc),
        last_scan_id=scan.id,
    )
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.flush()
    session.add(scan)
    session.flush()
    session.add(source)
    session.flush()
    if archived:
        volume = StorageVolume(id=f"volume-{source_id}", node_id="omv", mount_path_runtime="/archive/a", online=True, writable=True)
        session.add(volume)
        session.flush()
        session.add(LocalAsset(
            id=f"asset-{source_id}", source_recording_id=source.id, storage_volume_id=volume.id,
            relative_path="recordings/clip.mp4", verification_state="verified", lifecycle_state="present",
        ))
        session.flush()
    return source


def test_selects_confirmed_sources_without_archive_or_date_filter(session):
    _seed_source(session, "source-ok")
    _seed_source(session, "source-failed-scan", scan_state="failed")
    _seed_source(session, "source-archived", archived=True)
    rows = _select_sources(
        session,
        now=datetime(2026, 8, 3, 4, tzinfo=timezone.utc),
        zone=ZoneInfo("Asia/Shanghai"),
    )
    assert [row.id for row in rows] == ["source-archived", "source-ok"]


def test_selects_only_sources_on_or_after_nvr_scan_floor(session, monkeypatch):
    old = _seed_source(session, "source-old")
    old.source_start = datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc)
    old.source_end = datetime(2026, 9, 1, tzinfo=timezone.utc)
    current = _seed_source(session, "source-current")
    current.source_start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    current.source_end = datetime(2026, 9, 1, 0, 0, 10, tzinfo=timezone.utc)
    old_scan = session.get(ScanRun, old.last_scan_id)
    old_scan.query_start = datetime(2026, 8, 31, tzinfo=timezone.utc)
    old_scan.query_end = datetime(2026, 9, 1, tzinfo=timezone.utc)
    current_scan = session.get(ScanRun, current.last_scan_id)
    current_scan.query_start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    current_scan.query_end = datetime(2026, 9, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "scripts.generate_unarchived_thumbnails.get_settings",
        lambda: type("Settings", (), {"nvr_scan_min_date": date(2026, 9, 1)})(),
    )
    rows = _select_sources(
        session,
        now=datetime(2026, 9, 2, tzinfo=timezone.utc),
        zone=ZoneInfo("Asia/Shanghai"),
    )
    assert [row.id for row in rows] == ["source-current"]


def test_next_noon_catches_up_to_next_local_noon():
    zone = ZoneInfo("Asia/Shanghai")
    next_run = _next_noon(datetime(2026, 8, 3, 3, 30, tzinfo=timezone.utc), zone)
    assert next_run.isoformat() == "2026-08-03T12:00:00+08:00"
    next_run = _next_noon(datetime(2026, 8, 3, 5, 0, tzinfo=timezone.utc), zone)
    assert next_run.isoformat() == "2026-08-04T12:00:00+08:00"


def test_thumbnail_watch_uses_bounded_poll_interval():
    source = Path("scripts/generate_unarchived_thumbnails.py").read_text(encoding="utf-8")
    assert "time_module.sleep(max(1, settings.thumbnail_poll_seconds))" in source


def test_thumbnail_worker_has_a_release_scan_floor():
    compose = Path("compose.yaml").read_text(encoding="utf-8")
    assert 'HOME_VISION_NVR_SCAN_MIN_DATE: "${HOME_VISION_NVR_SCAN_MIN_DATE:-2026-09-01}"' in compose


def test_nvr_thumbnail_stream_is_bounded():
    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_bytes(self):
            yield b"1234"
            yield b"5678"

    class HttpClient:
        def stream(self, *_args, **_kwargs):
            return Response()

    client = object.__new__(HikvisionIsapiClient)
    client._base_url = "http://example.invalid/"
    client._client = HttpClient()
    client._download_method = "GET"
    with client.stream_download("rtsp://example.invalid/playback", max_bytes=5) as chunks:
        assert b"".join(chunks) == b"12345"
