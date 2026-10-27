from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import subprocess
import tempfile
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
            # Keep fixture encoding within the shared hosted/Vela test budget;
            # otherwise x264's automatic thread fan-out can fail before the
            # scenario's injected product path starts.
            "-shortest", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p",
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


def _git_secret_locations(repo: Path, secret: str, paths: tuple[str, ...]) -> set[str]:
    """Return only location categories, never matched lines or secret bytes."""
    locations: set[str] = set()
    grep = subprocess.run(
        ["git", "grep", "-F", "-l", secret, "--", *paths], cwd=repo,
        capture_output=True,
    )
    assert grep.returncode in (0, 1)
    if grep.returncode == 0:
        locations.add("worktree")
    staged = subprocess.run(
        ["git", "grep", "--cached", "-F", "-l", secret, "--", *paths], cwd=repo,
        capture_output=True,
    )
    assert staged.returncode in (0, 1)
    if staged.returncode == 0:
        locations.add("index")
    history = subprocess.run(
        ["git", "log", "--all", "--format=%H", "-S", secret, "--", *paths],
        cwd=repo, capture_output=True,
    )
    assert history.returncode == 0
    if history.stdout.strip():
        locations.add("history")
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", *paths],
        cwd=repo, capture_output=True, check=True,
    )
    for raw_path in untracked.stdout.split(b"\0"):
        if not raw_path:
            continue
        path = repo / raw_path.decode("utf-8", "surrogateescape")
        if secret in path.name or (path.is_file() and secret.encode() in path.read_bytes()):
            locations.add("untracked")
    return locations


def _files_containing_secret(root: Path, secret: str) -> list[str]:
    matches = []
    marker = secret.encode()
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if marker in path.read_bytes():
                matches.append(path.relative_to(root).as_posix())
        except OSError:
            continue
    return matches


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
            failed_at = job.completed_at
            assert failed_at is not None
            if failed_at.tzinfo is None:
                failed_at = failed_at.replace(tzinfo=timezone.utc)
            assert job.attempt_count == 1
            assert session.query(LocalAsset).filter_by(source_recording_id=source.id).count() == 0
            final_path = volume_root / "recordings" / start.strftime("%Y/%m/%d") / "camera-105" / f"{start:%H%M%S}-{source.id}.mp4"
            assert not final_path.exists()

            # Use the production retry defaults and prove both sides of the
            # first-backoff boundary instead of jumping straight to recovery.
            assert retry_due_archive_jobs(
                session, now=failed_at + timedelta(seconds=299),
            ) == 0
            session.refresh(job)
            assert job.state == "failed"
            assert session.query(AuditEvent).filter_by(
                entity_id=job.id, event_type="archive_retry_scheduled",
            ).count() == 0

            assert retry_due_archive_jobs(
                session, now=failed_at + timedelta(seconds=300),
            ) == 1
            session.commit()
            session.refresh(job)
            assert job.state == "queued"
            retry_events = list(session.scalars(select(AuditEvent).where(
                AuditEvent.entity_id == job.id,
                AuditEvent.event_type == "archive_retry_scheduled",
            )))
            assert len(retry_events) == 1
            assert json.loads(retry_events[0].payload_redacted) == {
                "attempt": 2, "previous_error_code": "NVR_BANDWIDTH_LIMIT",
            }
            assert archive_worker.process_one(
                session, client, probe=FfprobeMediaProbe(), worker_id="s005-worker",
            ) == job.id
            session.refresh(job)
            asset = session.scalar(select(LocalAsset).where(
                LocalAsset.source_recording_id == source.id,
            ))
            assert job.state == "completed"
            assert job.attempt_count == 2
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


def test_s036_optional_stage_failures_preserve_verified_archive(
    session, tmp_path, monkeypatch,
):
    """Inject each catalog S036 failure after archive and preserve archive truth."""
    import os
    import sys
    import math
    import struct
    import time
    import wave

    from app.domain.analysis_package import AnalysisPackageError, build_analysis_package, publish_immutable_package
    from app.domain.ffmpeg_frame_reducer import FfmpegLumaDifferenceReducer
    from app.domain.frame_reduce import load_working_frame_manifest
    from media_derivation.pipeline import DerivationPaths, DerivationPlan, run_derivation

    payload = _synthetic_video(tmp_path / "s036-optional-stages.mp4")
    now = datetime.now(timezone.utc)
    today = datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc)
    source_start = today - timedelta(hours=2)
    source_end = source_start + timedelta(seconds=3)
    nvr = NvrDevice(id="s036-stage-nvr", name="S036 synthetic", host="127.0.0.1", timezone="UTC")
    camera = CameraTrack(id="s036-stage-camera", nvr_id=nvr.id, track_id="101",
                         camera_name="S036 synthetic")
    session.add(nvr)
    session.flush()
    session.add(camera)
    volume_root = tmp_path / "s036-stage-volume"
    session.add(StorageVolume(
        id="s036-stage-volume", node_id="ci-fixture", mount_path_runtime=str(volume_root),
        total_bytes=2 * 1024**4, free_bytes=2 * 1024**4, online=True, writable=True,
    ))
    session.flush()

    recording = MockRecording("101", source_start, source_end, payload)
    with MockHikvisionNvr([recording]) as mock_nvr:
        with _client(mock_nvr) as client:
            for scan_id in ("s036-stage-scan-1", "s036-stage-scan-2"):
                scan = scan_camera_range(
                    session, nvr=nvr, camera=camera,
                    query_start=today - timedelta(days=1),
                    query_end=today + timedelta(days=1),
                    searcher=client, scan_id=scan_id,
                    scan_min_date=now.date() - timedelta(days=2),
                )
                assert (scan.state, scan.discovered_count) == ("success", 1)
                session.commit()
            queued = enqueue_closed_day_backlog(
                session, nvr_id=nvr.id, now=datetime.now(timezone.utc),
                enabled=True, timezone_verified=True,
            )
            assert queued.eligible_count == 1 and queued.queue_result is not None
            job = session.get(ArchiveJob, queued.queue_result.queued[0])
            assert job is not None and job.state == "queued"
            monkeypatch.setattr(archive_worker, "_touch_heartbeat", lambda *_args, **_kwargs: None)
            assert archive_worker.process_one(
                session, client, probe=FfprobeMediaProbe(), worker_id="s036-stage-worker",
            ) == job.id
            session.refresh(job)
            assert job.state == "completed"

    archive_asset = session.scalar(select(LocalAsset).where(
        LocalAsset.source_recording_id == job.source_recording_id,
    ))
    assert archive_asset is not None
    assert (archive_asset.verification_state, archive_asset.lifecycle_state) == ("verified", "present")
    archive_path = volume_root / archive_asset.relative_path
    archive_bytes = archive_path.read_bytes()
    archive_sha = hashlib.sha256(archive_bytes).hexdigest()
    assert archive_sha == hashlib.sha256(payload).hexdigest()

    timeline_path = tmp_path / "s036-stage-outcomes.jsonl"
    timeline = []

    def persist_failure(stage, started_at, error_code, *, retryable=None):
        finished_at = datetime.now(timezone.utc)
        outcome = {
            "stage": stage, "attempt": 1, "state": "failed",
            "started_at": started_at.isoformat(), "finished_at": finished_at.isoformat(),
            "error_code": error_code,
        }
        if retryable is not None:
            outcome["retryable"] = retryable
        with timeline_path.open("ab") as stream:
            stream.write((json.dumps(outcome, sort_keys=True, separators=(",", ":")) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        timeline.append(outcome)

    derivation_root = tmp_path / "s036-derived"
    paths = DerivationPaths(raw=archive_path, root=derivation_root)
    derived = run_derivation(
        paths, DerivationPlan(has_audio=True), source_start_epoch=time.time() - 3600,
    )
    with wave.open(str(paths.audio_final), "wb") as audio_file:
        audio_file.setnchannels(1)
        audio_file.setsampwidth(2)
        audio_file.setframerate(16_000)
        audio_file.writeframes(struct.pack(
            "<16000h", *(int(2000 * math.sin(2 * math.pi * 440 * i / 16_000))
                          for i in range(16_000)),
        ))
    derived_status = {
        "asset_id": derivation_root.name, "state": "derived",
        "stages": {"web": "done", "frames": "done", "audio": "done", "transcript": "idle"},
    }
    status_path = derivation_root / "status.json"
    status_path.write_text(json.dumps(derived_status), encoding="utf-8")

    asr_failure = tmp_path / "s036-asr-fail.py"
    asr_child_marker = tmp_path / "s036-asr-child-started"
    asr_failure.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[1]).write_text('started', encoding='utf-8')\n"
        "sys.exit(23)\n",
        encoding="utf-8",
    )
    asr_started = datetime.now(timezone.utc)
    asr = subprocess.run(
        [sys.executable, "-m", "media_derivation.transcribe_cli",
         "--asset-root", str(tmp_path), "--asset-id", derivation_root.name,
         "--local-asr-command", sys.executable, str(asr_failure), str(asr_child_marker)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=False, timeout=30,
    )
    assert asr.returncode == 1
    assert asr_child_marker.read_text(encoding="utf-8") == "started"
    asr_status = json.loads(status_path.read_text(encoding="utf-8"))
    assert asr_status["state"] == "derived"
    assert asr_status["stages"]["transcript"] == "failed"
    assert asr_status["transcript"]["retryable"] is True
    persist_failure(
        "asr", asr_started, asr_status["transcript"]["error_code"],
        retryable=asr_status["transcript"]["retryable"],
    )

    frames = load_working_frame_manifest(Path(derived["frame_manifest"]), asset_id=derivation_root.name)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    child_marker = tmp_path / "s036-reducer-child-started"
    fake_ffmpeg = fake_bin / "ffmpeg"
    fake_ffmpeg.write_text(
        "#!/bin/sh\nprintf started > \"$FRAME_REDUCE_CHILD_MARKER\"\nexec sleep 2\n",
        encoding="utf-8",
    )
    fake_ffmpeg.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FRAME_REDUCE_CHILD_MARKER", str(child_marker))
    reducer = FfmpegLumaDifferenceReducer(
        paths.frames_dir, change_threshold=0.1, timeout_seconds=0.05,
    )
    reduce_started = datetime.now(timezone.utc)
    try:
        reducer.reduce((frames[0],))
    except ValueError as exc:
        reduce_error = str(exc)
    else:
        pytest.fail("the injected reducer child timeout did not fail")
    assert reduce_error == "FRAME_REDUCE_DECODE_FAILED"
    assert child_marker.read_text(encoding="utf-8") == "started"
    persist_failure("frame_reduce", reduce_started, reduce_error)

    artifact_root = tmp_path / "s036-package-artifacts"
    artifact_root.mkdir()
    artifact = artifact_root / "transcript.json"
    artifact.write_text('{"schema":"p16.transcript.v1"}\n', encoding="utf-8")
    package = build_analysis_package(
        package_id="s036-package", asset_id=archive_asset.id,
        source_manifest={"source_sha256": archive_sha}, pipeline={"version": "synthetic"},
        artifacts=[{"artifact_id": "transcript", "artifact_type": "transcript",
                    "path": "transcript.json"}], artifact_root=artifact_root,
    )
    artifact.unlink()
    package_started = datetime.now(timezone.utc)
    try:
        publish_immutable_package(
            tmp_path / "s036-package", package, artifact_root=artifact_root,
        )
    except AnalysisPackageError as exc:
        package_error = str(exc)
    else:
        pytest.fail("publication accepted a package whose source artifact was removed")
    assert package_error == "PACKAGE_ARTIFACT_MISSING"
    persist_failure("analysis_package", package_started, package_error)

    persisted = [json.loads(line) for line in timeline_path.read_text(encoding="utf-8").splitlines()]
    assert [event["stage"] for event in persisted] == ["asr", "frame_reduce", "analysis_package"]
    assert persisted == timeline
    assert all(event["state"] == "failed" for event in persisted)
    assert persisted[0]["retryable"] is True
    assert all("retryable" not in event for event in persisted[1:])
    assert [event["error_code"] for event in persisted[1:]] == [
        "FRAME_REDUCE_DECODE_FAILED", "PACKAGE_ARTIFACT_MISSING",
    ]
    assert all(datetime.fromisoformat(event["finished_at"]) >= datetime.fromisoformat(event["started_at"])
               for event in persisted)
    assert not (tmp_path / "s036-package" / "analysis-package.json").exists()

    session.expire_all()
    remaining_assets = list(session.scalars(select(LocalAsset).where(
        LocalAsset.source_recording_id == job.source_recording_id,
    )))
    session.refresh(job)
    assert job.state == "completed"
    assert len(remaining_assets) == 1 and remaining_assets[0].sha256 == archive_sha
    assert archive_path.read_bytes() == archive_bytes
    assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == archive_sha
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


@pytest.mark.parametrize("fault", ["http200-xml", "truncated-stream", "corrupt-media"])
def test_s006_download_faults_quarantine_before_bounded_retry(
    session, tmp_path, monkeypatch, fault,
):
    """Reject interrupted, corrupt, and HTTP-200 error downloads before retry."""
    payload = _synthetic_video(tmp_path / "s006-synthetic.mp4")
    xml_error = (
        b"<?xml version='1.0'?><ResponseStatus><statusCode>4</statusCode></ResponseStatus>"
        + b" " * 2048
    )
    corrupt_media = b"synthetic corrupt media bytes\n" * 128
    truncate_after = max(1024, len(payload) // 2)
    if fault == "http200-xml":
        fixture_fault = {"error_payload_once": True}
        quarantined_bytes = xml_error
        expected_error = "DOWNLOAD_ERROR_PAYLOAD"
    elif fault == "truncated-stream":
        fixture_fault = {"download_truncate_after_bytes": [truncate_after]}
        quarantined_bytes = payload[:truncate_after]
        expected_error = "NVR_DOWNLOAD_FAILED"
    else:
        fixture_fault = {"download_payload_overrides": [corrupt_media]}
        quarantined_bytes = corrupt_media
        expected_error = "MEDIA_PROBE_FAILED"
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
        [MockRecording("101", source_start, source_end, payload)], **fixture_fault,
    ) as mock_nvr:
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
            assert (job.state, job.error_code) == ("failed", expected_error)
            assert session.scalar(select(LocalAsset).where(
                LocalAsset.source_recording_id == job.source_recording_id)) is None
            quarantined = list((volume_root / ".quarantine").glob(f"{job.id}.*.partial"))
            assert len(quarantined) == 1
            assert quarantined[0].read_bytes() == quarantined_bytes
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
    quarantined = list((volume_root / ".quarantine").glob(f"{job.id}.*.partial"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == quarantined_bytes
    # The retry publishes only the valid original video; failed bytes stay
    # quarantined and never become a canonical verified asset.
    assert asset.sha256 != hashlib.sha256(quarantined_bytes).hexdigest()
    assert session.query(AuditEvent).filter_by(
        event_type="archive_partial_quarantined", entity_id=job.id).count() == 1
    assert mock_nvr.download_count == 2


@pytest.mark.parametrize("case", ["valid", "bad-credentials", "wrong-nonce"])
def test_s010_digest_challenge_and_invalid_credentials_fail_closed(session, case):
    """S010's positive and two negative Digest controls execute as separate cases."""
    start = datetime.now(timezone.utc) - timedelta(days=2)
    nvr = NvrDevice(
        id=f"s010-{case}-nvr", name=f"S010 {case}", host="127.0.0.1", timezone="UTC"
    )
    camera = CameraTrack(
        id=f"s010-{case}-camera", nvr_id=nvr.id, track_id="101",
        camera_name=f"S010 {case}",
    )
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.flush()

    wrong_nonce = case == "wrong-nonce"
    bad_credentials = case == "bad-credentials"
    challenge_nonce = "synthetic-stale-nonce" if wrong_nonce else None
    with MockHikvisionNvr([], challenge_nonce=challenge_nonce) as mock:
        username = "synthetic-wrong-user" if bad_credentials else mock.username
        password = "synthetic-wrong-password" if bad_credentials else mock.password
        with HikvisionIsapiClient(
            base_url=mock.base_url, username=username, password=password,
            timeout_seconds=5,
        ) as client:
            result = scan_camera_range(
                session, nvr=nvr, camera=camera,
                query_start=start, query_end=start + timedelta(days=1),
                searcher=client, scan_id=f"s010-{case}-scan",
            )

        assert mock.challenge_count == 1
        assert mock.search_count == (1 if case == "valid" else 0)
        if case == "valid":
            assert (result.state, result.error_code) == ("success", None)
            assert mock.authorization_count == 1
            assert mock.rejected_digest_count == 0
            assert result.discovered_count == 0
        else:
            assert mock.authorization_count == 0
            assert mock.rejected_digest_count == 1
            assert mock.download_count == 0
            assert (result.state, result.error_code) == ("failed", "NVR_SEARCH_HTTP_403")
            scan = session.get(ScanRun, result.scan_id)
            assert scan is not None and scan.error_code == "NVR_SEARCH_HTTP_403"
            assert list(session.scalars(select(SourceRecording).where(
                SourceRecording.nvr_id == nvr.id))) == []
            if bad_credentials:
                assert "synthetic-wrong-password" not in repr(result)
            if wrong_nonce:
                assert "synthetic-stale-nonce" not in repr(result)


def test_component_authenticated_nvr_reflected_secret_is_redacted_everywhere(
    session, tmp_path, caplog, capsys,
):
    """A credential reflected by a hostile synthetic NVR stays out of P16 state."""
    secret = secrets.token_hex(32)
    nvr = NvrDevice(id="s027-nvr", name="S027 synthetic", host="127.0.0.1", timezone="UTC")
    camera = CameraTrack(id="s027-camera", nvr_id=nvr.id, track_id="101",
                         camera_name="S027 synthetic")
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.flush()
    start = datetime.now(timezone.utc) - timedelta(days=2)
    reflected_body = f"<error>diagnostic credential={secret}</error>".encode()

    with MockHikvisionNvr(
        [], password=secret, search_statuses=[500],
        search_error_bodies=[reflected_body],
    ) as mock_nvr:
        with HikvisionIsapiClient(
            base_url=mock_nvr.base_url, username=mock_nvr.username,
            password=mock_nvr.password, timeout_seconds=5,
        ) as client:
            result = scan_camera_range(
                session, nvr=nvr, camera=camera,
                query_start=start, query_end=start + timedelta(hours=1),
                searcher=client, scan_id="s027-reflected-secret-scan",
            )

        assert mock_nvr.challenge_count == 1
        assert mock_nvr.authorization_count == 1
        assert mock_nvr.search_count == 1

    assert (result.state, result.discovered_count, result.error_code) == (
        "failed", 0, "NVR_SEARCH_HTTP_500",
    )
    assert secret not in repr(result)
    scan = session.get(ScanRun, result.scan_id)
    assert scan is not None and scan.error_code == "NVR_SEARCH_HTTP_500"
    for model in (NvrDevice, CameraTrack, ScanRun, SourceRecording, ArchiveJob,
                  LocalAsset, AuditEvent):
        assert secret not in repr(list(session.scalars(select(model))))
    assert secret not in caplog.text
    captured = capsys.readouterr()
    assert secret not in captured.out and secret not in captured.err
    assert _files_containing_secret(tmp_path, secret) == []
    assert _git_secret_locations(
        Path(__file__).resolve().parents[2], secret,
        ("implementation", "design", "reports"),
    ) == set()

    # Mutation canary: the same independent scanner must catch a leak in
    # tracked bytes, the index, and reachable Git history without printing it.
    with tempfile.TemporaryDirectory(prefix="s027-git-leak-", dir=tmp_path) as raw_repo:
        repo = Path(raw_repo)
        source = repo / "implementation" / "synthetic-config.txt"
        source.parent.mkdir(parents=True)
        source.write_text(f"password={secret}\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "P16 synthetic"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "p16-synthetic@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "add", "implementation/synthetic-config.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "synthetic secret leak canary"], cwd=repo, check=True)
        assert _git_secret_locations(repo, secret, ("implementation",)) == {
            "worktree", "index", "history",
        }
    assert _files_containing_secret(tmp_path, secret) == []
    assert session.scalar(select(SourceRecording).where(SourceRecording.nvr_id == nvr.id)) is None
    assert session.scalar(select(ArchiveJob)) is None
    assert session.scalar(select(LocalAsset)) is None
