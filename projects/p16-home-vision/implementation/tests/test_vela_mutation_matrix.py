"""Exact, software-only canaries for mutations named by the frozen P16 soak catalog."""
from datetime import datetime, time, timedelta, timezone
import hashlib
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlalchemy import select
import pytest

from app.domain.auto_archive import closed_day_cutoff_utc, enqueue_closed_day_backlog
from app.domain.archive_pipeline import ArchiveValidationError, verify_part
from app.infra.media.ffprobe import FfprobeMediaProbe
from app.infra.nvr.hikvision import HikvisionIsapiClient
from app.models import ArchiveJob, CameraTrack, NvrDevice, ScanRun, SourceRecording
from app.domain.nvr_scan import scan_camera_range
from app.models import LocalAsset
from scripts.vela_soak_runtime import EvidenceStore, digest, record_daily_cycle


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


def test_s031_current_day_enqueue_canary_rejects_cutoff_mutant(session):
    """C1: the same no-early-enqueue oracle rejects the mutant and accepts product."""
    day = datetime.now(timezone.utc).date()
    now = datetime.combine(day, time(12), tzinfo=timezone.utc)
    expected_cutoff = closed_day_cutoff_utc(now=now, timezone_name="UTC")

    _, source = _seed_current_day_source(session, day, now)
    original_scalars = session.scalars

    def assert_current_day_not_queued(result, jobs):
        assert result.eligible_count == 0
        assert jobs == []

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
        # Independent UTC calendar oracle confirms this is a current-day source
        # whose end is after the real closed-day boundary.
        oracle_rejects = source.source_end >= expected_cutoff
        assert mutant_jobs and oracle_rejects
        # Prove the actual acceptance invariant would fail for the injected
        # product mutation; merely describing the mutant output can pass even
        # when the canary itself has no rejecting assertion.
        with pytest.raises(AssertionError):
            assert_current_day_not_queued(mutant_result, mutant_jobs)

    session.rollback()
    _seed_current_day_source(session, day, now)
    actual_result = _run(session, now)
    session.flush()
    actual_jobs = list(session.scalars(select(ArchiveJob)))
    assert actual_result.cutoff_utc == expected_cutoff
    assert_current_day_not_queued(actual_result, actual_jobs)


def test_s031_future_ending_video_canary_rejects_cutoff_mutant(session):
    """C2: a clip that crosses into tomorrow is never closed-day eligible."""
    from types import SimpleNamespace

    day = datetime.now(timezone.utc).date()
    now = datetime.combine(day, time(12), tzinfo=timezone.utc)
    expected_cutoff = closed_day_cutoff_utc(now=now, timezone_name="UTC")
    _, source = _seed_current_day_source(session, day, now)
    source.source_end = datetime.combine(day + timedelta(days=1), time(1),
                                         tzinfo=timezone.utc)
    source.duration_ms = 24 * 60 * 60 * 1000
    session.flush()

    def reject_future_source(result, jobs):
        assert result.eligible_count == 0
        assert jobs == []

    original_scalars = session.scalars

    def remove_closed_day_predicate(statement, *args, **kwargs):
        if (statement.column_descriptions
                and statement.column_descriptions[0].get("entity") is SourceRecording):
            # Inject the production query mutant: omit only source_end <= cutoff.
            rows = original_scalars(
                select(SourceRecording).where(
                    SourceRecording.nvr_id == "s031-nvr",
                    SourceRecording.source_state == "available",
                )
            ).all()
            return SimpleNamespace(all=lambda: rows)
        return original_scalars(statement, *args, **kwargs)

    with patch.object(session, "scalars", side_effect=remove_closed_day_predicate):
        mutant_result = _run(session, now)
        session.flush()
        mutant_jobs = list(original_scalars(select(ArchiveJob)))
        assert source.source_end > expected_cutoff
        assert mutant_jobs
        with pytest.raises(AssertionError):
            reject_future_source(mutant_result, mutant_jobs)

    session.rollback()
    _, source = _seed_current_day_source(session, day, now)
    source.source_end = datetime.combine(day + timedelta(days=1), time(1),
                                         tzinfo=timezone.utc)
    source.duration_ms = 24 * 60 * 60 * 1000
    session.flush()
    actual_result = _run(session, now)
    session.flush()
    actual_jobs = list(session.scalars(select(ArchiveJob)))
    assert actual_result.cutoff_utc == expected_cutoff
    reject_future_source(actual_result, actual_jobs)


def test_s031_incorrect_hash_canary_rejects_digest_mutant(tmp_path, monkeypatch):
    """A wrong digest returned by the production verifier cannot satisfy file truth."""
    from app.domain import archive_pipeline
    from app.infra.media.ffprobe import MediaProbeResult

    payload = b"synthetic verified video bytes" * 64
    part = tmp_path / "hash-canary.mp4.part"
    part.write_bytes(payload)

    class SyntheticProbe:
        def probe(self, path):
            return MediaProbeResult(
                container="mp4", video_codec="h264", audio_codec=None,
                width=16, height=16, duration_ms=180_000, has_video=True,
            )

    expected = hashlib.sha256(payload).hexdigest()
    original_digest = archive_pipeline.sha256_file

    def assert_file_hash_matches_product(digest):
        assert digest == hashlib.sha256(part.read_bytes()).hexdigest()

    # Kill a concrete product mutation at its digest boundary, then prove the
    # same file is accepted by the unmodified production verifier.
    monkeypatch.setattr(archive_pipeline, "sha256_file", lambda _path: "0" * 64)
    with pytest.raises(AssertionError):
        actual, _, _ = verify_part(
            part, expected_duration_ms=180_000, probe=SyntheticProbe(),
        )
        assert_file_hash_matches_product(actual)

    monkeypatch.setattr(archive_pipeline, "sha256_file", original_digest)
    actual, media, size = verify_part(
        part, expected_duration_ms=180_000, probe=SyntheticProbe(),
    )
    assert_file_hash_matches_product(actual)
    assert actual == expected
    assert media.has_video and size == len(payload)


def test_s031_duplicate_enqueue_canary_rejects_missing_active_job_guard(session):
    """A duplicate-active-job mutation is caught by persisted queue truth."""
    from sqlalchemy import delete
    from app.domain import archive_queue

    day = datetime.now(timezone.utc).date() - timedelta(days=1)
    now = datetime.combine(day, time(1), tzinfo=timezone.utc)
    _, source = _seed_current_day_source(session, day, now)

    # Establish the production invariant first: repeated enqueue yields one
    # job and reports the source as already queued.
    first = archive_queue.enqueue_archive_jobs(session, [source.id])
    second = archive_queue.enqueue_archive_jobs(session, [source.id])
    assert len(first.queued) == 1
    assert second.already_queued == (source.id,)
    assert session.query(ArchiveJob).filter_by(source_recording_id=source.id).count() == 1

    # Inject the missing active-job lookup and remove only the synthetic DB
    # partial index that otherwise independently masks this application bug.
    bind = session.get_bind()
    if bind.dialect.name != "sqlite":
        pytest.skip("the isolated duplicate mutant is defined for local SQLite")
    session.execute(delete(ArchiveJob).where(ArchiveJob.source_recording_id == source.id))
    session.flush()
    session.execute(__import__("sqlalchemy").text(
        "DROP INDEX IF EXISTS uq_archive_jobs_one_active_per_source"
    ))
    original_scalar = session.scalar

    class ConflictIgnoringInsert:
        def __init__(self, table):
            from sqlalchemy import insert
            self.statement = insert(table)

        def values(self, **values):
            self.statement = self.statement.values(**values)
            return self

        def on_conflict_do_nothing(self, **_kwargs):
            return self.statement

    def skip_active_job_guard(statement, *args, **kwargs):
        if ArchiveJob.__table__ in statement.get_final_froms():
            return None
        return original_scalar(statement, *args, **kwargs)

    with patch.object(session, "scalar", side_effect=skip_active_job_guard), \
            patch.object(archive_queue, "sqlite_insert", ConflictIgnoringInsert):
        mutant_first = archive_queue.enqueue_archive_jobs(session, [source.id])
        mutant_second = archive_queue.enqueue_archive_jobs(session, [source.id])
        session.flush()
        mutant_count = session.query(ArchiveJob).filter_by(source_recording_id=source.id).count()
        assert mutant_first.queued and mutant_second.queued
        with pytest.raises(AssertionError):
            assert mutant_count == 1

    session.rollback()


def test_s033_http_200_error_xml_and_invalid_video_are_rejected(session, tmp_path):
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


def test_s032_fifteen_day_history_is_immutable_across_recovery_and_restart(tmp_path):
    """Reconcile synthetic daily rows and prove prior failure evidence cannot be rewritten."""
    singapore = ZoneInfo("Asia/Singapore")
    today = datetime.now(singapore).date()
    first = today - timedelta(days=14)
    state_root = tmp_path / "s032"
    store = EvidenceStore(state_root / "evidence")
    failed_record = None
    for offset in range(15):
        day = first + timedelta(days=offset)
        run_id = f"s032-day-{offset:02d}"
        complete = offset != 5
        observed = datetime.combine(day, datetime.min.time(), tzinfo=singapore) + timedelta(minutes=15)
        ledger = record_daily_cycle(
            state_root, scheduled_start=first.isoformat(), observed_day=day.isoformat(),
            run_id=run_id, complete=complete, observed_at=observed.isoformat(),
        )
        record = {
            "run_id": run_id, "source_sha": ("a" if offset < 8 else "b") * 40,
            "scenario_id": "S032", "seed": "p16-g0-v1-S032",
            "timestamp_utc": observed.astimezone(timezone.utc).isoformat(),
            "status": "PASS" if complete else "FAIL",
            "result": {"duration_seconds": 1.0 + offset / 100},
        }
        store.append(record)
        if not complete:
            failed_record = record
        assert ledger["attempt"][0] == run_id

    # Preserve the original failure while recording a later linked recovery.
    recovery_day = first + timedelta(days=6)
    recovery_at = datetime.combine(recovery_day, datetime.min.time(), tzinfo=singapore) + timedelta(minutes=15)
    recovery = {
        "run_id": "s032-recovery", "source_sha": "b" * 40,
        "scenario_id": "S032", "seed": "p16-g0-v1-S032",
        "timestamp_utc": recovery_at.astimezone(timezone.utc).isoformat(),
        "status": "PASS", "recovery_of": failed_record["run_id"],
        "result": {"duration_seconds": 1.25},
    }
    store.append(recovery)

    with __import__("sqlite3").connect(state_root / "daily-ledger.sqlite3") as db:
        schedule = db.execute("SELECT day, state FROM daily_schedule ORDER BY day").fetchall()
        attempts = db.execute("SELECT run_id, outcome FROM daily_attempts ORDER BY run_id").fetchall()
    assert len(schedule) == 15
    assert [state for _, state in schedule].count("FAIL") == 1
    assert [state for _, state in schedule].count("PASS") == 14
    assert len(attempts) == 15

    run_files = sorted((state_root / "evidence" / "runs").glob("*.json"))
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in run_files}
    failed_path = next(path for path in run_files if path.stem == digest(failed_record))
    original_failure_bytes = failed_path.read_bytes()
    recovery_path = next(path for path in run_files if path.stem == digest(recovery))
    failed_path.write_bytes(recovery_path.read_bytes())
    with pytest.raises(ValueError, match="EVIDENCE_HASH_MISMATCH"):
        EvidenceStore(state_root / "evidence")
    failed_path.write_bytes(original_failure_bytes)

    restarted = EvidenceStore(state_root / "evidence")
    after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
             for path in (state_root / "evidence" / "runs").glob("*.json")}
    assert before == after
    recovered_records = restarted.records()
    assert len(recovered_records) == 16
    assert next(r for r in recovered_records if r["run_id"] == failed_record["run_id"])["status"] == "FAIL"
    assert next(r for r in recovered_records if r["run_id"] == recovery["run_id"])["recovery_of"] == failed_record["run_id"]
    daily_records = [r for r in recovered_records if r["run_id"].startswith("s032-day-")]
    assert len({r["timestamp_utc"][:10] for r in daily_records}) == 15
    assert {r["source_sha"] for r in daily_records} == {"a" * 40, "b" * 40}
    durations = sorted(r["result"]["duration_seconds"] for r in daily_records)
    p95 = durations[14]  # nearest-rank p95 for this frozen 15-record sample
    assert p95 == pytest.approx(1.14)


def test_s034_singapore_midnight_cutoff_and_nvr_floor_match_utc_oracle(session):
    """Execute a UTC-midnight mutant through enqueue and verify the scan floor."""
    from datetime import date
    from app.domain.nvr_scan import scan_camera_range
    from app.models import CameraTrack, NvrDevice, ScanRun

    singapore = ZoneInfo("Asia/Singapore")
    before_midnight = datetime(2026, 10, 26, 23, 59, tzinfo=singapore)
    at_midnight = datetime(2026, 10, 27, 0, 0, tzinfo=singapore)
    expected_before = datetime(2026, 10, 25, 16, 0, tzinfo=timezone.utc)
    expected_after = datetime(2026, 10, 26, 16, 0, tzinfo=timezone.utc)
    assert closed_day_cutoff_utc(now=before_midnight, timezone_name="Asia/Singapore") == expected_before
    assert closed_day_cutoff_utc(now=at_midnight, timezone_name="Asia/Singapore") == expected_after

    # Execute the UTC-date-midnight mutant against a real synthetic DB row.
    # This source ends in the 8-hour gap where UTC midnight wrongly considers
    # it closed, but Singapore midnight correctly considers it still open.
    nvr = NvrDevice(id="s034-nvr", name="S034 synthetic", host="127.0.0.1",
                    timezone="Asia/Singapore")
    session.add(nvr)
    session.flush()
    camera = CameraTrack(id="s034-camera", nvr_id=nvr.id, track_id="101",
                         camera_name="S034 synthetic")
    session.add(camera)
    session.flush()
    start = datetime(2026, 10, 25, 19, 0, tzinfo=timezone.utc)
    for index, completed in enumerate((
        before_midnight.astimezone(timezone.utc) - timedelta(minutes=2),
        before_midnight.astimezone(timezone.utc) - timedelta(minutes=1),
    ), start=1):
        session.add(ScanRun(
            id=f"s034-scan-{index}", nvr_id=nvr.id, track_id=camera.track_id,
            query_start=start - timedelta(days=1), query_end=start + timedelta(days=1),
            state="success", result_count=1, response_hash="s034-stable",
            started_at=completed - timedelta(seconds=10), completed_at=completed,
        ))
    session.flush()
    source = SourceRecording(
        id="s034-gap-source", nvr_id=nvr.id, track_id=camera.track_id,
        source_start=start, source_end=datetime(2026, 10, 25, 20, 0, tzinfo=timezone.utc),
        duration_ms=60_000, locator_fingerprint="synthetic-s034",
        first_scan_id="s034-scan-1", last_scan_id="s034-scan-2",
    )
    session.add(source)
    session.flush()

    utc_midnight_mutant = lambda *, now, timezone_name: datetime.combine(
        now.astimezone(timezone.utc).date(), time.min, tzinfo=timezone.utc
    )
    with patch("app.domain.auto_archive.closed_day_cutoff_utc", utc_midnight_mutant):
        mutant_result = enqueue_closed_day_backlog(
            session, nvr_id=nvr.id, now=before_midnight,
            enabled=True, timezone_verified=True,
        )
        session.flush()
        mutant_jobs = list(session.scalars(select(ArchiveJob)))
        independent_local_day_oracle_rejects = source.source_end > expected_before
        assert mutant_result.cutoff_utc == datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc)
        assert mutant_result.eligible_count == 1
        assert len(mutant_jobs) == 1
        assert independent_local_day_oracle_rejects

    session.rollback()
    nvr = NvrDevice(id="s034-nvr", name="S034 synthetic", host="127.0.0.1",
                    timezone="Asia/Singapore")
    session.add(nvr)
    session.flush()
    camera = CameraTrack(id="s034-camera", nvr_id=nvr.id, track_id="101",
                         camera_name="S034 synthetic")
    session.add(camera)
    session.flush()
    for index, completed in enumerate((
        before_midnight.astimezone(timezone.utc) - timedelta(minutes=2),
        before_midnight.astimezone(timezone.utc) - timedelta(minutes=1),
    ), start=1):
        session.add(ScanRun(
            id=f"s034-scan-{index}", nvr_id=nvr.id, track_id=camera.track_id,
            query_start=start - timedelta(days=1), query_end=start + timedelta(days=1),
            state="success", result_count=1, response_hash="s034-stable",
            started_at=completed - timedelta(seconds=10), completed_at=completed,
        ))
    session.flush()
    source = SourceRecording(
        id="s034-gap-source", nvr_id=nvr.id, track_id=camera.track_id,
        source_start=start, source_end=datetime(2026, 10, 25, 20, 0, tzinfo=timezone.utc),
        duration_ms=60_000, locator_fingerprint="synthetic-s034",
        first_scan_id="s034-scan-1", last_scan_id="s034-scan-2",
    )
    session.add(source)
    session.flush()
    actual_result = enqueue_closed_day_backlog(
        session, nvr_id=nvr.id, now=before_midnight,
        enabled=True, timezone_verified=True,
    )
    session.flush()
    assert actual_result.cutoff_utc == expected_before
    assert actual_result.eligible_count == 0
    assert list(session.scalars(select(ArchiveJob))) == []

    class EmptySearcher:
        def search_history(self, _request):
            return []

    result = scan_camera_range(
        session, nvr=nvr, camera=camera,
        query_start=datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc),
        query_end=datetime(2026, 10, 27, 16, 0, tzinfo=timezone.utc),
        searcher=EmptySearcher(), scan_id="s034-floor",
        scan_min_date=date(2026, 10, 26),
    )
    scan = session.get(ScanRun, result.scan_id)
    assert scan is not None
    assert scan.query_start.replace(tzinfo=timezone.utc) == expected_before
