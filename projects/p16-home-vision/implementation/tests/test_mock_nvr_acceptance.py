from __future__ import annotations

import hashlib
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker

from app.domain.archive_pipeline import ArchiveValidationError, verify_part
from app.domain.archive_queue import enqueue_archive_jobs, retry_due_archive_jobs, retry_failed_archive_jobs
from app.domain.auto_archive import enqueue_closed_day_backlog
from app.domain.analysis_contract import AnalysisRequest, ModelRunnerError
from analysis_worker.model_runner import FailingFixtureModelRunner
from app.domain.nvr_scan import scan_camera_range
from app.infra.media.ffprobe import FfprobeMediaProbe, MediaProbeResult
from app.infra.nvr.hikvision import HikvisionIsapiClient, build_search_request_xml
from app.models import (
    ArchiveJob,
    AuditEvent,
    CameraTrack,
    LocalAsset,
    NvrDevice,
    ScanRun,
    SourceRecording,
    StorageVolume,
)
from scripts import archive_worker
from tests.support.mock_hikvision_nvr import MockHikvisionNvr, MockRecording


def _synthetic_video(path: Path) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.fail("P16 mock NVR acceptance requires the CI ffmpeg installation")
    subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=10:d=3",
            "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path.read_bytes()


def _client(nvr: MockHikvisionNvr) -> HikvisionIsapiClient:
    return HikvisionIsapiClient(
        base_url=nvr.base_url,
        username=nvr.username,
        password=nvr.password,
        timeout_seconds=5,
        download_method="POST",
    )


def test_s004_repeated_scan_enqueues_one_verified_asset(session, tmp_path, monkeypatch):
    """Repeated discovery/enqueue is idempotent and creates one verified asset."""
    payload = _synthetic_video(tmp_path / "synthetic.mp4")
    now = datetime.now(timezone.utc)
    today = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    old_start = today - timedelta(hours=2)
    old_end = old_start + timedelta(seconds=3)
    current_start = today + timedelta(hours=1)
    current_end = current_start + timedelta(seconds=3)
    nvr = NvrDevice(id="nvr-mock", name="Synthetic Hikvision", host="127.0.0.1", timezone="UTC")
    camera = CameraTrack(id="camera-mock", nvr_id=nvr.id, track_id="101", camera_name="Fixture Camera")
    session.add(nvr)
    session.flush()
    session.add(camera)
    volume_root = tmp_path / "archive-volume"
    session.add(StorageVolume(
        id="volume-mock", node_id="ci-fixture", mount_path_runtime=str(volume_root),
        total_bytes=2 * 1024**4, free_bytes=2 * 1024**4, online=True, writable=True,
    ))
    session.flush()

    recordings = [
        MockRecording("101", old_start, old_end, payload),
        MockRecording("101", current_start, current_end, payload),
    ]
    with MockHikvisionNvr(recordings) as mock_nvr:
        with _client(mock_nvr) as client:
            for scan_id in ("mock-scan-1", "mock-scan-2"):
                result = scan_camera_range(
                    session,
                    nvr=nvr,
                    camera=camera,
                    query_start=today - timedelta(days=1),
                    query_end=today + timedelta(days=1),
                    searcher=client,
                    scan_id=scan_id,
                    scan_min_date=now.date() - timedelta(days=2),
                )
                assert (result.state, result.discovered_count) == ("success", 2)
                session.commit()

            sources = list(session.scalars(select(SourceRecording).where(SourceRecording.nvr_id == nvr.id)))
            assert len(sources) == 2  # repeated discovery reconciles to stable source identities
            old_source = next(s for s in sources if s.source_start.replace(tzinfo=timezone.utc) == old_start)
            today_source = next(s for s in sources if s.source_start.replace(tzinfo=timezone.utc) == current_start)
            assert old_source.last_scan_id == "mock-scan-2"
            assert today_source.last_scan_id == "mock-scan-2"

            auto = enqueue_closed_day_backlog(
                session, nvr_id=nvr.id, now=datetime.now(timezone.utc),
                enabled=True, timezone_verified=True,
            )
            session.flush()
            assert auto.eligible_count == 1
            job = session.scalar(select(ArchiveJob).where(ArchiveJob.source_recording_id == old_source.id))
            assert job is not None and job.state == "queued"
            assert session.scalar(select(ArchiveJob).where(ArchiveJob.source_recording_id == today_source.id)) is None

            repeated = enqueue_closed_day_backlog(
                session, nvr_id=nvr.id, now=datetime.now(timezone.utc),
                enabled=True, timezone_verified=True,
            )
            session.flush()
            assert repeated.queue_result is not None
            assert repeated.queue_result.queued == ()
            assert repeated.queue_result.already_queued == (old_source.id,)

            monkeypatch.setattr(archive_worker, "_touch_heartbeat", lambda *_args, **_kwargs: None)
            completed_id = archive_worker.process_one(
                session, client, probe=FfprobeMediaProbe(), worker_id="mock-worker"
            )
            assert completed_id == job.id
            session.refresh(job)
            assert job.state == "completed"
            assert mock_nvr.search_count == 3  # two scans and one source resolve
            assert mock_nvr.download_count == 1
            assert mock_nvr.authorization_count >= 4

    asset = session.scalar(select(LocalAsset).where(LocalAsset.source_recording_id == old_source.id))
    assert asset is not None
    assert asset.verification_state == "verified"
    assert asset.lifecycle_state == "present"
    assert asset.sha256 == hashlib.sha256(payload).hexdigest()
    final_path = volume_root / asset.relative_path
    assert final_path.is_file()
    assert final_path.read_bytes() == payload
    assert not final_path.with_suffix(final_path.suffix + ".part").exists()
    probed = FfprobeMediaProbe().probe(final_path)
    assert probed.has_video and probed.duration_ms == pytest.approx(3000, abs=150)
    assert session.query(LocalAsset).filter_by(source_recording_id=old_source.id).count() == 1

    # C3 canary: removing the verified-asset lookup would make a completed
    # source eligible for a second job. Keep that mutation inside a rollback
    # boundary, and compare it to the independent DB cardinality oracle.
    original_scalar = session.scalar

    def omit_verified_asset_lookup(statement, *args, **kwargs):
        descriptions = getattr(statement, "column_descriptions", ())
        if descriptions and descriptions[0].get("entity") is LocalAsset:
            return None
        return original_scalar(statement, *args, **kwargs)

    nested = session.begin_nested()
    try:
        with patch.object(session, "scalar", side_effect=omit_verified_asset_lookup):
            mutant = enqueue_archive_jobs(session, [old_source.id])
            session.flush()
            mutant_job_count = session.query(ArchiveJob).filter_by(
                source_recording_id=old_source.id
            ).count()
            asset_count = session.query(LocalAsset).filter_by(
                source_recording_id=old_source.id
            ).count()
        assert mutant.queued and mutant_job_count == 2 and asset_count == 1
        assert asset_count == 1  # already archived, so another job is invalid
    finally:
        nested.rollback()

    session.expire_all()
    actual = enqueue_archive_jobs(session, [old_source.id])
    session.flush()
    assert actual.already_archived == (old_source.id,)
    assert session.query(ArchiveJob).filter_by(source_recording_id=old_source.id).count() == 1
    assert session.query(LocalAsset).filter_by(source_recording_id=old_source.id).count() == 1
    assert "fixture-password" not in repr(old_source)
    assert all("fixture-password" not in (event.payload_redacted or "") for event in session.query(AuditEvent).all())


def test_s005_http_453_is_retryable_only_after_explicit_backoff(session, tmp_path, monkeypatch):
    """Inject one real mock-NVR 453 response and verify bounded archive recovery."""
    payload = _synthetic_video(tmp_path / "s005-source.mp4")
    now = datetime.now(timezone.utc)
    start = datetime.combine(now.date() - timedelta(days=2), time(1), tzinfo=timezone.utc)
    end = start + timedelta(seconds=3)
    nvr = NvrDevice(id="s005-nvr", name="S005 synthetic", host="127.0.0.1", timezone="UTC")
    camera = CameraTrack(id="s005-camera", nvr_id=nvr.id, track_id="105", camera_name="Synthetic")
    volume_root = tmp_path / "s005-volume"
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.add(StorageVolume(
        id="s005-volume", node_id="fixture", mount_path_runtime=str(volume_root),
        total_bytes=2 * 1024**4, free_bytes=2 * 1024**4, online=True, writable=True,
    ))
    session.flush()
    recording = MockRecording(camera.track_id, start, end, payload)

    with MockHikvisionNvr([recording], download_statuses=[453]) as mock_nvr:
        with _client(mock_nvr) as client:
            scan = scan_camera_range(
                session, nvr=nvr, camera=camera,
                query_start=start - timedelta(hours=1),
                query_end=end + timedelta(hours=1), searcher=client,
                scan_id="s005-scan",
            )
            assert (scan.state, scan.discovered_count) == ("success", 1)
            session.commit()
            source = session.scalar(select(SourceRecording).where(
                SourceRecording.nvr_id == nvr.id,
                SourceRecording.track_id == camera.track_id,
            ))
            assert source is not None
            queued = enqueue_archive_jobs(session, [source.id])
            assert len(queued.queued) == 1
            session.commit()
            job = session.scalar(select(ArchiveJob).where(
                ArchiveJob.source_recording_id == source.id,
            ))
            monkeypatch.setattr(archive_worker, "_touch_heartbeat", lambda *_args, **_kwargs: None)

            assert archive_worker.process_one(
                session, client, probe=FfprobeMediaProbe(), worker_id="s005-worker",
            ) == job.id
            session.refresh(job)
            # Independent contract oracle: the first response is a throttle,
            # not a successful payload and not an automatic tight-loop retry.
            assert (job.state, job.error_code) == ("failed", "NVR_BANDWIDTH_LIMIT")
            assert session.query(LocalAsset).filter_by(source_recording_id=source.id).count() == 0
            final_path = volume_root / "recordings" / start.strftime("%Y/%m/%d") / "camera-105" / f"{start:%H%M%S}-{source.id}.mp4"
            assert not final_path.exists()

            assert retry_due_archive_jobs(
                session, now=datetime.now(timezone.utc) + timedelta(hours=1),
            ) == 1
            session.commit()
            assert archive_worker.process_one(
                session, client, probe=FfprobeMediaProbe(), worker_id="s005-worker",
            ) == job.id
            session.refresh(job)
            asset = session.scalar(select(LocalAsset).where(
                LocalAsset.source_recording_id == source.id,
            ))
            assert job.state == "completed"
            assert asset is not None and asset.verification_state == "verified"
            assert asset.sha256 == hashlib.sha256(payload).hexdigest()
            assert (volume_root / asset.relative_path).read_bytes() == payload
            assert session.query(LocalAsset).filter_by(source_recording_id=source.id).count() == 1
            assert mock_nvr.download_count == 2


def test_two_workers_racing_one_source_create_one_verified_asset(session, tmp_path, monkeypatch):
    """S017 injects two claim readers before either can promote the same job."""
    nvr = NvrDevice(id="nvr-s017", name="S017 synthetic", host="fixture", timezone="UTC")
    camera = CameraTrack(id="camera-s017", nvr_id=nvr.id, track_id="s017", camera_name="Synthetic")
    source_start = datetime.now(timezone.utc) - timedelta(days=2)
    source_end = source_start + timedelta(seconds=10)
    source = SourceRecording(
        id="source-s017", nvr_id=nvr.id, track_id=camera.track_id,
        source_start=source_start, source_end=source_end, duration_ms=10_000,
        source_state="available",
    )
    volume_root = tmp_path / "s017-volume"
    session.add(nvr); session.flush(); session.add(camera); session.flush()
    session.add(source); session.flush()
    session.add(StorageVolume(
        id="volume-s017", node_id="fixture", mount_path_runtime=str(volume_root),
        total_bytes=2 * 1024**4, free_bytes=2 * 1024**4, online=True, writable=True,
    ))
    job = ArchiveJob(id="job-s017", source_recording_id=source.id, state="queued")
    session.add(job)
    session.commit()

    engine = session.get_bind()
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    both_claim_reads = threading.Barrier(2)
    payload = b"synthetic-video-payload" * 128
    segment = SimpleNamespace(
        track_id=source.track_id, source_start=source_start, source_end=source_end,
        duration_ms=10_000, source_size_if_known=None, playback_uri="fixture://s017",
    )

    class FixtureClient:
        def download_to_part(self, _uri, path, *, progress_callback=None):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            if progress_callback:
                progress_callback(len(payload))
            return len(payload)

    class FixtureProbe:
        def probe(self, _path):
            return MediaProbeResult(duration_ms=10_000, has_video=True, container="mp4",
                                    video_codec="h264", audio_codec="aac")

    def synchronize_claim_read(_conn, _cursor, statement, _parameters, _context, _executemany):
        sql = statement.lower()
        if "from archive_jobs join source_recordings" in sql and "archive_jobs.state = ?" in sql:
            both_claim_reads.wait(timeout=10)

    monkeypatch.setattr(archive_worker, "_touch_heartbeat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(archive_worker, "_find_exact_segment", lambda *_args, **_kwargs: segment)
    event.listen(engine, "after_cursor_execute", synchronize_claim_read)
    try:
        def run_worker(worker_id):
            with sessions() as worker_session:
                return archive_worker.process_one(
                    worker_session, FixtureClient(), probe=FixtureProbe(), worker_id=worker_id,
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run_worker, ("s017-worker-a", "s017-worker-b")))
    finally:
        event.remove(engine, "after_cursor_execute", synchronize_claim_read)

    assert results.count(job.id) == 1
    assert results.count(None) == 1
    session.expire_all()
    stored_job = session.get(ArchiveJob, job.id)
    assets = list(session.scalars(select(LocalAsset).where(
        LocalAsset.source_recording_id == source.id,
        LocalAsset.verification_state == "verified",
        LocalAsset.lifecycle_state == "present",
    )))
    assert stored_job is not None and stored_job.state == "completed"
    assert stored_job.attempt_count == 1
    assert len(assets) == 1
    final_path = volume_root / assets[0].relative_path
    assert final_path.is_file()
    assert hashlib.sha256(final_path.read_bytes()).hexdigest() == assets[0].sha256


def test_simulated_nvr_xml_error_payload_is_rejected(tmp_path):
    payload = b"synthetic payload is not used because the NVR returns an ISAPI error"
    start = datetime.now(timezone.utc) - timedelta(days=1)
    recording = MockRecording("101", start, start + timedelta(seconds=3), payload)
    part = tmp_path / "bad-download.part"
    with MockHikvisionNvr([recording], error_payload_once=True) as mock_nvr:
        with _client(mock_nvr) as client:
            segment = client.search_history(build_search_request_xml(
                track_id="101", query_start=start - timedelta(seconds=1),
                query_end=start + timedelta(seconds=4),
            ))[0]
            received = client.download_to_part(segment.playback_uri, part)
    assert received > 0
    with pytest.raises(ArchiveValidationError, match="DOWNLOAD_ERROR_PAYLOAD"):
        verify_part(part, expected_duration_ms=3000, probe=FfprobeMediaProbe())
    assert part.exists()  # caller/worker quarantine owns failure recovery; verifier never promotes it


def test_simulated_nvr_rejects_mutating_http_methods():
    import httpx

    with MockHikvisionNvr([]) as mock_nvr:
        response = httpx.put(f"{mock_nvr.base_url}/ISAPI/ContentMgmt/search", timeout=5)
    assert response.status_code == 405


def test_model_runner_timeout_does_not_block_synthetic_archive(session, tmp_path, monkeypatch):
    """ModelRunner failure leaves media archiving independent; not the S036 matrix."""
    payload = _synthetic_video(tmp_path / "s036-synthetic.mp4")
    now = datetime.now(timezone.utc)
    today = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    source_start = today - timedelta(hours=2)
    source_end = source_start + timedelta(seconds=3)
    nvr = NvrDevice(id="s036-nvr", name="S036 synthetic", host="127.0.0.1", timezone="UTC")
    camera = CameraTrack(id="s036-camera", nvr_id=nvr.id, track_id="101",
                         camera_name="S036 synthetic")
    session.add(nvr)
    session.flush()
    session.add(camera)
    volume_root = tmp_path / "s036-volume"
    session.add(StorageVolume(
        id="s036-volume", node_id="ci-fixture", mount_path_runtime=str(volume_root),
        total_bytes=2 * 1024**4, free_bytes=2 * 1024**4, online=True, writable=True,
    ))
    session.flush()

    recording = MockRecording("101", source_start, source_end, payload)
    with MockHikvisionNvr([recording]) as mock_nvr:
        with _client(mock_nvr) as client:
            for scan_id in ("s036-scan-1", "s036-scan-2"):
                scanned = scan_camera_range(
                    session, nvr=nvr, camera=camera,
                    query_start=today - timedelta(days=1),
                    query_end=today + timedelta(days=1),
                    searcher=client, scan_id=scan_id,
                    scan_min_date=now.date() - timedelta(days=2),
                )
                assert (scanned.state, scanned.discovered_count) == ("success", 1)
                session.commit()

            auto = enqueue_closed_day_backlog(
                session, nvr_id=nvr.id, now=datetime.now(timezone.utc),
                enabled=True, timezone_verified=True,
            )
            assert auto.eligible_count == 1
            job = session.get(ArchiveJob, auto.queue_result.queued[0])
            assert job is not None and job.state == "queued"

            request = AnalysisRequest(
                run_id="s036-analysis", asset_id="pending-media-asset", profile_id="s036",
                profile_revision=1, profile_snapshot_hash="synthetic-profile-hash",
                semantic_intent=None, output_schema_json=None,
                media_ref="synthetic://s036", evidence=(),
            )
            with pytest.raises(ModelRunnerError, match="MODEL_TIMEOUT"):
                FailingFixtureModelRunner("MODEL_TIMEOUT").analyze(request)
            assert job.state == "queued"

            monkeypatch.setattr(archive_worker, "_touch_heartbeat", lambda *_args, **_kwargs: None)
            processed = archive_worker.process_one(
                session, client, probe=FfprobeMediaProbe(), worker_id="s036-worker")
            assert processed == job.id
            session.refresh(job)
            assert job.state == "completed"

    asset = session.scalar(select(LocalAsset).where(
        LocalAsset.source_recording_id == job.source_recording_id))
    assert asset is not None
    assert (asset.verification_state, asset.lifecycle_state) == ("verified", "present")
    assert asset.sha256 == hashlib.sha256(payload).hexdigest()
    assert (volume_root / asset.relative_path).read_bytes() == payload
    assert mock_nvr.search_count == 3
    assert mock_nvr.download_count == 1


def test_s036_asr_reduce_package_failures_leave_verified_archive_unchanged(
    session, tmp_path, monkeypatch,
):
    """Exercise all S036 optional-stage failures against a real synthetic archive."""
    import json
    import sys

    from app.domain.analysis_package import AnalysisPackageError, build_analysis_package
    from app.domain.ffmpeg_frame_reducer import FfmpegLumaDifferenceReducer
    from app.domain.frame_reduce import load_working_frame_manifest
    from media_derivation import cli as derivation_cli, transcribe_cli
    from media_derivation.local_speech import JsonCommandASR as RealJsonCommandASR

    source_path = tmp_path / "s036-matrix-source.mp4"
    generated = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=10:d=3",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=3",
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart", str(source_path),
        ], check=False, capture_output=True, timeout=120,
    )
    assert generated.returncode == 0, generated.stderr.decode(errors="replace")[-1000:]
    payload = source_path.read_bytes()
    now = datetime.now(timezone.utc)
    today = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    source_start = today - timedelta(hours=2)
    source_end = source_start + timedelta(seconds=3)
    nvr = NvrDevice(id="s036-matrix-nvr", name="S036 synthetic", host="127.0.0.1", timezone="UTC")
    camera = CameraTrack(id="s036-matrix-camera", nvr_id=nvr.id, track_id="101",
                         camera_name="S036 synthetic")
    session.add(nvr)
    session.flush()
    session.add(camera)
    volume_root = tmp_path / "s036-matrix-volume"
    session.add(StorageVolume(
        id="s036-matrix-volume", node_id="ci-fixture", mount_path_runtime=str(volume_root),
        total_bytes=2 * 1024**4, free_bytes=2 * 1024**4, online=True, writable=True,
    ))
    session.flush()

    recording = MockRecording("101", source_start, source_end, payload)
    with MockHikvisionNvr([recording]) as mock_nvr:
        with _client(mock_nvr) as client:
            for scan_id in ("s036-matrix-scan-1", "s036-matrix-scan-2"):
                scanned = scan_camera_range(
                    session, nvr=nvr, camera=camera,
                    query_start=today - timedelta(days=1),
                    query_end=today + timedelta(days=1), searcher=client,
                    scan_id=scan_id, scan_min_date=now.date() - timedelta(days=2),
                )
                assert (scanned.state, scanned.discovered_count) == ("success", 1)
                session.commit()
            auto = enqueue_closed_day_backlog(
                session, nvr_id=nvr.id, now=datetime.now(timezone.utc),
                enabled=True, timezone_verified=True,
            )
            assert auto.eligible_count == 1
            job = session.get(ArchiveJob, auto.queue_result.queued[0])
            assert job is not None and job.state == "queued"
            monkeypatch.setattr(archive_worker, "_touch_heartbeat", lambda *_a, **_kw: None)
            assert archive_worker.process_one(
                session, client, probe=FfprobeMediaProbe(), worker_id="s036-matrix-worker",
            ) == job.id

    asset = session.scalar(select(LocalAsset).where(
        LocalAsset.source_recording_id == job.source_recording_id,
    ))
    assert asset is not None
    archived_path = volume_root / asset.relative_path
    expected_archive_hash = hashlib.sha256(payload).hexdigest()
    assert (asset.verification_state, asset.lifecycle_state, asset.sha256) == (
        "verified", "present", expected_archive_hash,
    )
    assert hashlib.sha256(archived_path.read_bytes()).hexdigest() == expected_archive_hash

    def assert_archive_unchanged():
        session.refresh(asset)
        assert (asset.verification_state, asset.lifecycle_state, asset.sha256) == (
            "verified", "present", expected_archive_hash,
        )
        assert hashlib.sha256(archived_path.read_bytes()).hexdigest() == expected_archive_hash

    timeline = []

    # Derive the same verified LocalAsset through the real FFmpeg production CLI.
    derived_root = tmp_path / "s036-derived"
    derived_asset = derived_root / asset.id
    monkeypatch.setattr(sys, "argv", [
        "p16-media-derive", "--raw", str(archived_path), "--asset-root", str(derived_root),
        "--asset-id", asset.id, "--camera", "synthetic-s036",
    ])
    assert derivation_cli.main() == 0
    status_path = derived_asset / "status.json"
    derived_status = json.loads(status_path.read_text())
    assert derived_status["asset_id"] == asset.id
    assert derived_status["state"] == "derived"
    assert derived_status["stages"]["audio"] == "done"
    sleeper = tmp_path / "s036-asr-timeout.py"
    sleeper.write_text("import time; time.sleep(2); print('[]')\n")
    monkeypatch.setattr(transcribe_cli, "JsonCommandASR", lambda command, *, backend_id:
                        RealJsonCommandASR(command, backend_id=backend_id, timeout_seconds=0.05))
    monkeypatch.setattr(sys, "argv", [
        "p16-media-transcribe", "--asset-root", str(derived_root), "--asset-id", asset.id,
        "--local-asr-command", sys.executable, str(sleeper),
    ])
    asr_exit_code = transcribe_cli.main()
    assert asr_exit_code == 1
    asr_status = json.loads(status_path.read_text())
    assert asr_status["state"] == "derived"
    assert asr_status["stages"]["transcript"] == "failed"
    assert asr_status["transcript"]["retryable"] is True
    assert asr_status["transcript"]["attempts"] == 1
    timeline.append({
        "stage": "ASR",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "return_code": asr_exit_code,
        "asset_state": asr_status["state"],
        "transcript_state": asr_status["stages"]["transcript"],
        "retryable": asr_status["transcript"]["retryable"],
        "attempts": asr_status["transcript"]["attempts"],
    })
    assert_archive_unchanged()

    # Production reducer: removing an actual PTS-backed working image triggers
    # an input error. Only disposable derived working data is affected.
    frames = load_working_frame_manifest(
        Path(derived_status["result"]["frame_manifest"]), asset_id=asset.id,
    )
    assert frames and all((derived_asset / "frames" / frame.file_name).is_file() for frame in frames)
    (derived_asset / "frames" / frames[0].file_name).unlink()
    reducer = FfmpegLumaDifferenceReducer(derived_asset / "frames", change_threshold=0.1)
    with pytest.raises(ValueError, match="FRAME_REDUCE_PATH_UNSAFE") as reduce_failure:
        reducer.reduce(frames)
    timeline.append({
        "stage": "Reduce",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "exception_type": type(reduce_failure.value).__name__,
        "exception": str(reduce_failure.value),
    })
    assert_archive_unchanged()

    # Production package builder: a missing transcript artifact is rejected
    # before publication, without changing the independently archived source.
    with pytest.raises(AnalysisPackageError, match="PACKAGE_ARTIFACT_MISSING") as package_failure:
        build_analysis_package(
            package_id="s036-package", asset_id=asset.id,
            source_manifest={"source_id": asset.source_recording_id}, pipeline={"version": "test"},
            artifacts=[{"artifact_id": "missing-transcript", "artifact_type": "transcript",
                        "path": "transcript/missing.json"}],
            artifact_root=derived_asset,
        )
    timeline.append({
        "stage": "Package",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "exception_type": type(package_failure.value).__name__,
        "exception": str(package_failure.value),
    })
    assert_archive_unchanged()
    assert [row["stage"] for row in timeline] == ["ASR", "Reduce", "Package"]
    timeline_path = tmp_path / "s036-stage-timeline.json"
    timeline_path.write_text(json.dumps(timeline, sort_keys=True) + "\n")
    persisted_timeline = json.loads(timeline_path.read_text())
    assert persisted_timeline == timeline
    observed_times = [datetime.fromisoformat(row["observed_at_utc"]) for row in persisted_timeline]
    assert observed_times == sorted(observed_times)
    assert persisted_timeline[0]["return_code"] == 1
    assert persisted_timeline[0]["transcript_state"] == "failed"
    assert persisted_timeline[0]["retryable"] is True
    assert persisted_timeline[1]["exception"] == "FRAME_REDUCE_PATH_UNSAFE"
    assert persisted_timeline[2]["exception"] == "PACKAGE_ARTIFACT_MISSING"
    final_status = json.loads(status_path.read_text())
    assert final_status["state"] == "derived"
    assert final_status["stages"]["web"] == final_status["stages"]["audio"] == "done"
    assert final_status["stages"]["transcript"] == "failed"
    assert final_status["stages"]["reduce"] == "queued"
    assert final_status["stages"]["package"] == "awaiting_analysis"
    assert mock_nvr.search_count == 3
    assert mock_nvr.download_count == 1


def test_s001_two_camera_closed_day_archives_only_closed_segments(session, tmp_path, monkeypatch):
    """S001: discover two cameras, preserve only closed-day sources, archive both."""
    payload = _synthetic_video(tmp_path / "s001-synthetic.mp4")
    now = datetime.now(timezone.utc)
    today = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    elapsed = (now - today).total_seconds()
    assert elapsed >= 2, "synthetic current-day segment requires a non-future interval"
    current_start = today + timedelta(seconds=elapsed * 0.5)
    current_end = today + timedelta(seconds=elapsed * 0.75)
    old_start = today - timedelta(hours=2)
    old_end = old_start + timedelta(seconds=3)
    assert old_end <= now and current_end <= now

    nvr = NvrDevice(id="s001-nvr", name="S001 synthetic", host="127.0.0.1", timezone="UTC")
    session.add(nvr)
    session.flush()
    cameras = []
    for track in ("101", "102"):
        camera = CameraTrack(id=f"s001-camera-{track}", nvr_id=nvr.id,
                             track_id=track, camera_name=f"S001 camera {track}")
        session.add(camera)
        cameras.append(camera)
    volume_root = tmp_path / "s001-volume"
    session.add(StorageVolume(
        id="s001-volume", node_id="ci-fixture", mount_path_runtime=str(volume_root),
        total_bytes=2 * 1024**4, free_bytes=2 * 1024**4, online=True, writable=True,
    ))
    session.flush()

    recordings = []
    for track in ("101", "102"):
        recordings.extend([
            MockRecording(track, old_start, old_end, payload),
            MockRecording(track, current_start, current_end, payload),
        ])
    with MockHikvisionNvr(recordings) as mock_nvr:
        with _client(mock_nvr) as client:
            for scan_number in (1, 2):
                for camera in cameras:
                    scanned = scan_camera_range(
                        session, nvr=nvr, camera=camera,
                        query_start=today - timedelta(days=1),
                        query_end=today + timedelta(days=1),
                        searcher=client, scan_id=f"s001-scan-{scan_number}-{camera.track_id}",
                        scan_min_date=now.date() - timedelta(days=2),
                    )
                    assert (scanned.state, scanned.discovered_count) == ("success", 2)
                    session.commit()

            auto = enqueue_closed_day_backlog(
                session, nvr_id=nvr.id, now=datetime.now(timezone.utc),
                enabled=True, timezone_verified=True,
            )
            assert auto.eligible_count == 2
            assert auto.queue_result is not None
            queued_ids = set(auto.queue_result.queued)
            assert len(queued_ids) == 2
            queued_jobs = list(session.scalars(select(ArchiveJob).where(
                ArchiveJob.id.in_(queued_ids))))
            assert {job.source_recording_id for job in queued_jobs} == {
                source.id for source in session.scalars(select(SourceRecording).where(
                    SourceRecording.nvr_id == nvr.id,
                    SourceRecording.source_end <= auto.cutoff_utc))
            }
            assert len(list(session.scalars(select(SourceRecording).where(
                SourceRecording.nvr_id == nvr.id,
                SourceRecording.source_end > auto.cutoff_utc)))) == 2

            monkeypatch.setattr(archive_worker, "_touch_heartbeat", lambda *_args, **_kwargs: None)
            processed = {
                archive_worker.process_one(session, client, probe=FfprobeMediaProbe(),
                                           worker_id="s001-worker")
                for _ in range(2)
            }
            assert processed == queued_ids
            for job in queued_jobs:
                session.refresh(job)
                assert job.state == "completed"

    assets = list(session.scalars(select(LocalAsset).where(
        LocalAsset.source_recording_id.in_([job.source_recording_id for job in queued_jobs]))))
    assert len(assets) == 2
    assert all((asset.verification_state, asset.lifecycle_state) == ("verified", "present")
               for asset in assets)
    assert all(asset.sha256 == hashlib.sha256(payload).hexdigest() for asset in assets)
    assert all((volume_root / asset.relative_path).read_bytes() == payload for asset in assets)
    assert mock_nvr.search_count == 6
    assert mock_nvr.download_count == 2


def test_s002_cross_midnight_growth_waits_for_stable_closed_day_scans(session, tmp_path, monkeypatch):
    """S002: do not archive a growing cross-midnight clip before a stable closed day."""
    payload = _synthetic_video(tmp_path / "s002-synthetic.mp4")
    actual_now = datetime.now(timezone.utc)
    today = datetime.combine(actual_now.date(), time.min, tzinfo=timezone.utc)
    start_day = today - timedelta(days=2)
    crossing_day = start_day + timedelta(days=1)
    source_start = start_day + timedelta(hours=23, minutes=58)
    partial_end = start_day + timedelta(hours=23, minutes=59, seconds=30)
    final_end = crossing_day + timedelta(minutes=2)
    nvr = NvrDevice(id="s002-nvr", name="S002 synthetic", host="127.0.0.1", timezone="UTC")
    camera = CameraTrack(id="s002-camera", nvr_id=nvr.id, track_id="101",
                         camera_name="S002 synthetic")
    session.add(nvr)
    session.flush()
    session.add(camera)
    volume_root = tmp_path / "s002-volume"
    session.add(StorageVolume(
        id="s002-volume", node_id="ci-fixture", mount_path_runtime=str(volume_root),
        total_bytes=2 * 1024**4, free_bytes=2 * 1024**4, online=True, writable=True,
    ))
    session.flush()

    partial = MockRecording("101", source_start, partial_end, payload)
    final = MockRecording("101", source_start, final_end, payload)
    with MockHikvisionNvr([partial]) as mock_nvr:
        with _client(mock_nvr) as client:
            def scan(scan_id):
                result = scan_camera_range(
                    session, nvr=nvr, camera=camera,
                    query_start=start_day - timedelta(days=1),
                    query_end=final_end + timedelta(days=1),
                    searcher=client, scan_id=scan_id,
                    scan_min_date=actual_now.date() - timedelta(days=4),
                )
                assert (result.state, result.discovered_count) == ("success", 1)
                session.commit()
                return result

            first_scan = scan("s002-scan-partial")
            source = session.scalar(select(SourceRecording).where(SourceRecording.nvr_id == nvr.id))
            assert source is not None
            source_id = source.id
            before_midnight = enqueue_closed_day_backlog(
                session, nvr_id=nvr.id,
                now=start_day + timedelta(days=0, hours=23, minutes=59, seconds=45),
                enabled=True, timezone_verified=True,
            )
            assert before_midnight.eligible_count == 0
            assert first_scan.scan_id == source.last_scan_id
            assert session.scalar(select(ArchiveJob).where(
                ArchiveJob.source_recording_id == source_id)) is None

            mock_nvr.recordings[:] = [final]
            second_scan = scan("s002-scan-final-first-observation")
            session.refresh(source)
            assert source.id == source_id
            assert source.source_end.replace(tzinfo=timezone.utc) == final_end
            after_crossing = enqueue_closed_day_backlog(
                session, nvr_id=nvr.id,
                now=crossing_day + timedelta(minutes=3),
                enabled=True, timezone_verified=True,
            )
            assert after_crossing.eligible_count == 0
            assert session.scalar(select(ArchiveJob).where(
                ArchiveJob.source_recording_id == source_id)) is None

            third_scan = scan("s002-scan-final-stable")
            assert second_scan.scan_id != third_scan.scan_id
            closed = enqueue_closed_day_backlog(
                session, nvr_id=nvr.id, now=actual_now,
                enabled=True, timezone_verified=True,
            )
            assert closed.eligible_count == 1
            assert closed.queue_result is not None and len(closed.queue_result.queued) == 1
            job_id = closed.queue_result.queued[0]
            monkeypatch.setattr(archive_worker, "_touch_heartbeat", lambda *_args, **_kwargs: None)
            processed = archive_worker.process_one(
                session, client, probe=FfprobeMediaProbe(), worker_id="s002-worker")
            assert processed == job_id
            job = session.get(ArchiveJob, job_id)
            assert job is not None and job.state == "completed"

    asset = session.scalar(select(LocalAsset).where(LocalAsset.source_recording_id == source_id))
    assert asset is not None and asset.verification_state == "verified"
    assert asset.sha256 == hashlib.sha256(payload).hexdigest()
    assert (volume_root / asset.relative_path).read_bytes() == payload
    assert mock_nvr.search_count == 4
    assert mock_nvr.download_count == 1


def test_s003_three_day_two_camera_backfill_has_no_archive_omissions(session, tmp_path, monkeypatch):
    """S003: cover every source in a bounded multi-day synthetic backfill."""
    payload = _synthetic_video(tmp_path / "s003-synthetic.mp4")
    now = datetime.now(timezone.utc)
    today = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    nvr = NvrDevice(id="s003-nvr", name="S003 synthetic", host="127.0.0.1", timezone="UTC")
    session.add(nvr)
    session.flush()
    cameras = []
    for track in ("101", "102"):
        camera = CameraTrack(id=f"s003-camera-{track}", nvr_id=nvr.id,
                             track_id=track, camera_name=f"S003 camera {track}")
        session.add(camera)
        cameras.append(camera)
    volume_root = tmp_path / "s003-volume"
    session.add(StorageVolume(
        id="s003-volume", node_id="ci-fixture", mount_path_runtime=str(volume_root),
        total_bytes=2 * 1024**4, free_bytes=2 * 1024**4, online=True, writable=True,
    ))
    session.flush()

    recordings = []
    for days_back in (2, 3, 4):
        start = today - timedelta(days=days_back) + timedelta(hours=12)
        end = start + timedelta(seconds=3)
        assert end <= now
        for track in ("101", "102"):
            recordings.append(MockRecording(track, start, end, payload))

    with MockHikvisionNvr(recordings) as mock_nvr:
        with _client(mock_nvr) as client:
            for scan_number in (1, 2):
                for camera in cameras:
                    scanned = scan_camera_range(
                        session, nvr=nvr, camera=camera,
                        query_start=today - timedelta(days=5), query_end=today,
                        searcher=client, scan_id=f"s003-scan-{scan_number}-{camera.track_id}",
                        scan_min_date=now.date() - timedelta(days=6),
                    )
                    assert (scanned.state, scanned.discovered_count) == ("success", 3)
                    session.commit()

            sources = list(session.scalars(select(SourceRecording).where(
                SourceRecording.nvr_id == nvr.id)))
            assert len(sources) == 6
            assert len({source.id for source in sources}) == 6
            assert len({(source.track_id, source.source_start) for source in sources}) == 6
            auto = enqueue_closed_day_backlog(
                session, nvr_id=nvr.id, now=datetime.now(timezone.utc),
                enabled=True, timezone_verified=True,
            )
            assert auto.eligible_count == 6
            assert auto.queue_result is not None
            queued_ids = set(auto.queue_result.queued)
            assert len(queued_ids) == 6
            queued_jobs = list(session.scalars(select(ArchiveJob).where(
                ArchiveJob.id.in_(queued_ids))))
            assert {job.source_recording_id for job in queued_jobs} == {s.id for s in sources}

            monkeypatch.setattr(archive_worker, "_touch_heartbeat", lambda *_args, **_kwargs: None)
            processed = [archive_worker.process_one(
                session, client, probe=FfprobeMediaProbe(), worker_id="s003-worker")
                for _ in range(6)]
            assert set(processed) == queued_ids
            for job in queued_jobs:
                session.refresh(job)
                assert job.state == "completed"

    assets = list(session.scalars(select(LocalAsset).where(
        LocalAsset.source_recording_id.in_([source.id for source in sources]))))
    assert len(assets) == 6
    assert all((asset.verification_state, asset.lifecycle_state) == ("verified", "present")
               for asset in assets)
    assert all(asset.sha256 == hashlib.sha256(payload).hexdigest() for asset in assets)
    assert all((volume_root / asset.relative_path).read_bytes() == payload for asset in assets)
    assert mock_nvr.search_count == 10
    assert mock_nvr.download_count == 6


@pytest.mark.parametrize("fault", ["xml-error", "truncated-stream", "corrupt-media"])
def test_s006_invalid_download_part_is_quarantined_before_bounded_retry(
    session, tmp_path, monkeypatch, fault,
):
    """S006: reject XML, truncated, and corrupt downloads before safe recovery."""
    payload = _synthetic_video(tmp_path / "s006-synthetic.mp4")
    now = datetime.now(timezone.utc)
    today = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    source_start = today - timedelta(hours=2)
    source_end = source_start + timedelta(seconds=3)
    nvr = NvrDevice(id="s006-nvr", name="S006 synthetic", host="127.0.0.1", timezone="UTC")
    camera = CameraTrack(id="s006-camera", nvr_id=nvr.id, track_id="101",
                         camera_name="S006 synthetic")
    session.add(nvr)
    session.flush()
    session.add(camera)
    volume_root = tmp_path / "s006-volume"
    session.add(StorageVolume(
        id="s006-volume", node_id="ci-fixture", mount_path_runtime=str(volume_root),
        total_bytes=2 * 1024**4, free_bytes=2 * 1024**4, online=True, writable=True,
    ))
    session.flush()

    with MockHikvisionNvr(
            [MockRecording("101", source_start, source_end, payload)],
            error_payload_once=fault == "xml-error",
            truncate_download_bytes_once=128 if fault == "truncated-stream" else None,
            corrupt_media_once=fault == "corrupt-media") as mock_nvr:
        with _client(mock_nvr) as client:
            for scan_id in ("s006-scan-1", "s006-scan-2"):
                scanned = scan_camera_range(
                    session, nvr=nvr, camera=camera,
                    query_start=today - timedelta(days=1),
                    query_end=today + timedelta(days=1),
                    searcher=client, scan_id=scan_id,
                    scan_min_date=now.date() - timedelta(days=2),
                )
                assert (scanned.state, scanned.discovered_count) == ("success", 1)
                session.commit()
            auto = enqueue_closed_day_backlog(
                session, nvr_id=nvr.id, now=datetime.now(timezone.utc),
                enabled=True, timezone_verified=True)
            assert auto.eligible_count == 1
            assert auto.queue_result is not None and len(auto.queue_result.queued) == 1
            job = session.get(ArchiveJob, auto.queue_result.queued[0])
            assert job is not None and job.state == "queued"

            monkeypatch.setattr(archive_worker, "_touch_heartbeat", lambda *_args, **_kwargs: None)
            assert archive_worker.process_one(
                session, client, probe=FfprobeMediaProbe(), worker_id="s006-worker") == job.id
            session.refresh(job)
            expected_error = {
                "xml-error": "DOWNLOAD_ERROR_PAYLOAD",
                "truncated-stream": "NVR_DOWNLOAD_FAILED",
                "corrupt-media": "MEDIA_PROBE_FAILED",
            }[fault]
            assert (job.state, job.error_code) == ("failed", expected_error)
            assert session.scalar(select(LocalAsset).where(
                LocalAsset.source_recording_id == job.source_recording_id)) is None
            quarantined = list((volume_root / ".quarantine").glob(f"{job.id}.*.partial"))
            assert len(quarantined) == 1
            quarantined_bytes = quarantined[0].read_bytes()
            if fault == "xml-error":
                assert quarantined_bytes.startswith(b"<?xml")
            elif fault == "truncated-stream":
                assert quarantined_bytes == payload[:128]
            else:
                assert quarantined_bytes.startswith(b"not-a-video")
            assert session.query(AuditEvent).filter_by(
                event_type="archive_partial_quarantined", entity_id=job.id).count() == 1

            retry = retry_failed_archive_jobs(session, actor="s006-synthetic-human-retry")
            assert len(retry.queued) == 1
            session.commit()
            retry_job = session.get(ArchiveJob, retry.queued[0])
            assert retry_job is not None and retry_job.state == "queued"
            assert archive_worker.process_one(
                session, client, probe=FfprobeMediaProbe(), worker_id="s006-worker") == retry_job.id
            session.refresh(retry_job)
            session.refresh(job)
            assert retry_job.state == "completed"
            assert (job.state, job.error_code) == ("failed", expected_error)

    asset = session.scalar(select(LocalAsset).where(
        LocalAsset.source_recording_id == retry_job.source_recording_id))
    assert asset is not None and asset.verification_state == "verified"
    assert asset.sha256 == hashlib.sha256(payload).hexdigest()
    assert (volume_root / asset.relative_path).read_bytes() == payload
    assert len(list((volume_root / ".quarantine").glob(f"{job.id}.*.partial"))) == 1
    assert mock_nvr.download_count == 2


def test_s010_digest_challenge_and_invalid_credentials_fail_closed(session):
    """S010: exercise real HTTP Digest 401 challenge and rejected credentials."""
    nvr = NvrDevice(id="s010-nvr", name="S010 synthetic", host="127.0.0.1", timezone="UTC")
    camera = CameraTrack(id="s010-camera", nvr_id=nvr.id, track_id="101",
                         camera_name="S010 synthetic")
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.flush()
    start = datetime.now(timezone.utc) - timedelta(days=2)

    with MockHikvisionNvr([]) as mock_nvr:
        with HikvisionIsapiClient(
            base_url=mock_nvr.base_url, username="synthetic-wrong-user",
            password="synthetic-wrong-password", timeout_seconds=5,
        ) as client:
            result = scan_camera_range(
                session, nvr=nvr, camera=camera,
                query_start=start, query_end=start + timedelta(days=1),
                searcher=client, scan_id="s010-rejected-scan",
            )
        assert mock_nvr.challenge_count == 1
        assert mock_nvr.rejected_digest_count == 1
        assert mock_nvr.authorization_count == 0
        assert mock_nvr.search_count == 0

    assert (result.state, result.error_code) == ("failed", "NVR_SEARCH_HTTP_403")
    assert "synthetic-wrong-password" not in repr(result)
    scan = session.get(ScanRun, result.scan_id)
    assert scan is not None and scan.error_code == "NVR_SEARCH_HTTP_403"
    assert list(session.scalars(select(SourceRecording).where(
        SourceRecording.nvr_id == nvr.id))) == []


    # The catalogued wrong-nonce branch uses valid credentials, so the server
    # must reject the Digest response specifically because it signed the
    # advertised nonce while the fixture expects a different nonce.
    nonce_nvr = NvrDevice(
        id="s010-nonce-nvr", name="S010 wrong nonce", host="127.0.0.1", timezone="UTC",
    )
    nonce_camera = CameraTrack(
        id="s010-nonce-camera", nvr_id=nonce_nvr.id, track_id="101",
        camera_name="S010 wrong nonce",
    )
    session.add(nonce_nvr)
    session.flush()
    session.add(nonce_camera)
    session.flush()
    with MockHikvisionNvr([], challenge_nonce="p16-advertised-wrong-nonce") as mock_nvr:
        assert mock_nvr.challenge_nonce != mock_nvr.nonce
        with HikvisionIsapiClient(
            base_url=mock_nvr.base_url, username=mock_nvr.username,
            password=mock_nvr.password, timeout_seconds=5,
        ) as client:
            result = scan_camera_range(
                session, nvr=nonce_nvr, camera=nonce_camera,
                query_start=start, query_end=start + timedelta(days=1),
                searcher=client, scan_id="s010-wrong-nonce-scan",
            )
        assert mock_nvr.challenge_count == 1
        assert mock_nvr.rejected_digest_count == 1
        assert mock_nvr.authorization_count == 0
        assert mock_nvr.search_count == 0
        assert mock_nvr.download_count == 0

    assert (result.state, result.error_code) == ("failed", "NVR_SEARCH_HTTP_403")
    scan = session.get(ScanRun, result.scan_id)
    assert scan is not None and scan.error_code == "NVR_SEARCH_HTTP_403"
    assert list(session.scalars(select(SourceRecording).where(
        SourceRecording.nvr_id == nonce_nvr.id))) == []
    assert list(session.scalars(select(ArchiveJob))) == []
    assert list(session.scalars(select(LocalAsset))) == []
