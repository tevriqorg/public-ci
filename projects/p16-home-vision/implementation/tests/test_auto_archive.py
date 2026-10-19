from datetime import datetime, timedelta, timezone

from app.domain.auto_archive import closed_day_cutoff_utc, enqueue_closed_day_backlog
from app.models import ArchiveJob, CameraTrack, NvrDevice, ScanRun, SourceRecording


def test_closed_day_cutoff_uses_configured_nvr_timezone() -> None:
    # 2026-10-19 15:59 UTC is 23:59 in Shanghai; local midnight is 16:00 UTC.
    now = datetime(2026, 10, 19, 15, 59, tzinfo=timezone.utc)
    assert closed_day_cutoff_utc(now=now, timezone_name="Asia/Shanghai") == datetime(
        2026, 10, 18, 16, 0, tzinfo=timezone.utc
    )


def test_auto_archive_queues_only_available_closed_day_backlog(session) -> None:
    nvr = NvrDevice(id="nvr-auto", name="NVR", host="fixture", timezone="Asia/Shanghai")
    camera = CameraTrack(id="camera-auto", nvr_id=nvr.id, track_id="101", camera_name="Camera")
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.flush()

    eligible = SourceRecording(
        id="source-eligible", nvr_id=nvr.id, track_id=camera.track_id,
        source_start=datetime(2026, 10, 18, 15, 0, tzinfo=timezone.utc),
        source_end=datetime(2026, 10, 18, 15, 59, tzinfo=timezone.utc),
        duration_ms=59_000, source_state="available",
    )
    today = SourceRecording(
        id="source-today", nvr_id=nvr.id, track_id=camera.track_id,
        source_start=datetime(2026, 10, 19, 16, 1, tzinfo=timezone.utc),
        source_end=datetime(2026, 10, 19, 16, 10, tzinfo=timezone.utc),
        duration_ms=9_000, source_state="available",
    )
    expired = SourceRecording(
        id="source-expired", nvr_id=nvr.id, track_id=camera.track_id,
        source_start=datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc),
        source_end=datetime(2026, 10, 17, 16, 0, tzinfo=timezone.utc),
        duration_ms=60_000, source_state="expired",
    )
    session.add_all([eligible, today, expired])
    session.flush()

    cutoff = datetime(2026, 10, 19, 16, 0, tzinfo=timezone.utc)
    first_scan = ScanRun(
        id="scan-stable-1", nvr_id=nvr.id, track_id=camera.track_id,
        query_start=datetime(2026, 10, 17, 16, 0, tzinfo=timezone.utc),
        query_end=datetime(2026, 10, 20, 16, 0, tzinfo=timezone.utc),
        state="success", result_count=1, response_hash="stable-hash",
        completed_at=cutoff + timedelta(minutes=5),
    )
    session.add(first_scan)
    session.flush()

    unstable = enqueue_closed_day_backlog(
        session,
        nvr_id=nvr.id,
        now=datetime(2026, 10, 19, 18, 0, tzinfo=timezone.utc),
        enabled=True,
        timezone_verified=True,
    )
    assert unstable.eligible_count == 0
    assert session.query(ArchiveJob).count() == 0

    session.add(ScanRun(
        id="scan-stable-2", nvr_id=nvr.id, track_id=camera.track_id,
        query_start=datetime(2026, 10, 17, 16, 0, tzinfo=timezone.utc),
        query_end=datetime(2026, 10, 20, 16, 0, tzinfo=timezone.utc),
        state="success", result_count=1, response_hash="stable-hash",
        completed_at=cutoff + timedelta(minutes=15),
    ))
    session.flush()

    # Identical camera/day hashes are insufficient if the latest scan did not
    # rediscover this particular ledger source.
    stale = enqueue_closed_day_backlog(
        session,
        nvr_id=nvr.id,
        now=datetime(2026, 10, 19, 18, 0, tzinfo=timezone.utc),
        enabled=True,
        timezone_verified=True,
    )
    assert stale.eligible_count == 0
    eligible.last_scan_id = "scan-stable-2"
    session.flush()

    result = enqueue_closed_day_backlog(
        session,
        nvr_id=nvr.id,
        now=datetime(2026, 10, 19, 18, 0, tzinfo=timezone.utc),
        enabled=True,
        timezone_verified=True,
    )
    session.flush()

    assert result.enabled
    assert result.eligible_count == 1
    assert result.queue_result is not None
    assert len(result.queue_result.queued) == 1
    assert session.query(ArchiveJob).filter_by(source_recording_id=eligible.id).count() == 1
    assert session.query(ArchiveJob).filter_by(source_recording_id=today.id).count() == 0
    assert session.query(ArchiveJob).filter_by(source_recording_id=expired.id).count() == 0

    second = enqueue_closed_day_backlog(
        session,
        nvr_id=nvr.id,
        now=datetime(2026, 10, 19, 18, 5, tzinfo=timezone.utc),
        enabled=True,
        timezone_verified=True,
    )
    assert second.queue_result is not None
    assert second.queue_result.queued == ()
    assert second.queue_result.already_queued == (eligible.id,)


def test_auto_archive_fails_closed_without_explicit_enable_or_timezone_verification(session) -> None:
    disabled = enqueue_closed_day_backlog(
        session, nvr_id="missing", enabled=False, timezone_verified=True
    )
    unverified = enqueue_closed_day_backlog(
        session, nvr_id="missing", enabled=True, timezone_verified=False
    )
    assert disabled.reason == "AUTO_ARCHIVE_DISABLED"
    assert unverified.reason == "NVR_TIMEZONE_NOT_VERIFIED"


def test_invalid_timezone_is_rejected() -> None:
    try:
        closed_day_cutoff_utc(now=datetime.now(timezone.utc), timezone_name="not/a-zone")
    except ValueError as exc:
        assert str(exc) == "NVR_TIMEZONE_INVALID"
    else:
        raise AssertionError("invalid configured NVR timezone must fail closed")
