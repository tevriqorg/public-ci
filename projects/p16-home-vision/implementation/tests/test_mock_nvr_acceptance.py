from __future__ import annotations

import hashlib
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker

from app.domain.archive_pipeline import ArchiveValidationError, verify_part
from app.domain.archive_queue import retry_due_archive_jobs
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


def test_simulated_nvr_closed_day_scan_retry_and_verified_archive(session, tmp_path, monkeypatch):
    """Exercise real HTTP/Digest, scan, closed-day enqueue, retry and ffprobe promotion."""
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
    with MockHikvisionNvr(recordings, download_statuses=[453]) as mock_nvr:
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
            failed_id = archive_worker.process_one(
                session, client, probe=FfprobeMediaProbe(), worker_id="mock-worker"
            )
            assert failed_id == job.id
            session.refresh(job)
            assert job.state == "failed"
            assert job.error_code == "NVR_BANDWIDTH_LIMIT"
            assert session.query(LocalAsset).filter_by(source_recording_id=old_source.id).count() == 0

            retried = retry_due_archive_jobs(
                session, now=datetime.now(timezone.utc) + timedelta(hours=1)
            )
            session.commit()
            assert retried == 1
            assert session.get(ArchiveJob, job.id).state == "queued"

            completed_id = archive_worker.process_one(
                session, client, probe=FfprobeMediaProbe(), worker_id="mock-worker"
            )
            assert completed_id == job.id
            session.refresh(job)
            assert job.state == "completed"
            assert mock_nvr.search_count == 4  # two scans and one source resolve for each attempt
            assert mock_nvr.download_count == 2
            assert mock_nvr.authorization_count >= 6

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
    assert "fixture-password" not in repr(old_source)
    assert all("fixture-password" not in (event.payload_redacted or "") for event in session.query(AuditEvent).all())


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


def test_s036_model_timeout_does_not_block_synthetic_archive(session, tmp_path, monkeypatch):
    """S036: fail Intelligence first, then complete the independent media archive path."""
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
