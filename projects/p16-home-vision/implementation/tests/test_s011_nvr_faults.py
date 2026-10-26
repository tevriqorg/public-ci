from __future__ import annotations

import hashlib
import socket
import shutil
import subprocess
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.domain.archive_queue import retry_due_archive_jobs
from app.domain.auto_archive import enqueue_closed_day_backlog
from app.domain.nvr_scan import scan_camera_range
from app.infra.media.ffprobe import FfprobeMediaProbe
from app.infra.nvr.hikvision import HikvisionIsapiClient
from app.models import ArchiveJob, CameraTrack, LocalAsset, NvrDevice, SourceRecording, StorageVolume
from scripts import archive_worker
from tests.support.mock_hikvision_nvr import MockHikvisionNvr, MockRecording


def _synthetic_video(path: Path) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.fail("S011 requires the public CI ffmpeg installation")
    subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=10:d=3",
            "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
            "-shortest", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path.read_bytes()


def _setup(session, tmp_path: Path, case: str):
    nvr = NvrDevice(
        id=f"s011-nvr-{case}", name=f"S011 {case}", host="127.0.0.1", timezone="UTC"
    )
    camera = CameraTrack(
        id=f"s011-camera-{case}", nvr_id=nvr.id, track_id="101",
        camera_name=f"S011 {case}",
    )
    volume_root = tmp_path / f"s011-volume-{case}"
    volume = StorageVolume(
        id=f"s011-volume-row-{case}", node_id="fixture",
        mount_path_runtime=str(volume_root), total_bytes=2 * 1024**4,
        free_bytes=2 * 1024**4, online=True, writable=True,
    )
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.add(volume)
    session.flush()
    now = datetime.now(timezone.utc)
    today = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    start = today - timedelta(days=2) + timedelta(hours=12)
    recording = MockRecording("101", start, start + timedelta(seconds=3), b"")
    return nvr, camera, volume, volume_root, recording, now


def _discover_and_queue(session, nvr, camera, now, client, recording):
    for index in range(2):
        result = scan_camera_range(
            session, nvr=nvr, camera=camera,
            query_start=recording.start - timedelta(days=1),
            query_end=recording.end + timedelta(days=1),
            searcher=client, scan_id=f"s011-scan-{nvr.id}-{index}",
            scan_min_date=now.date() - timedelta(days=5),
        )
        assert (result.state, result.discovered_count) == ("success", 1)
        session.commit()
    queued = enqueue_closed_day_backlog(
        session, nvr_id=nvr.id, now=datetime.now(timezone.utc),
        enabled=True, timezone_verified=True,
    )
    assert queued.eligible_count == 1
    assert queued.queue_result is not None and len(queued.queue_result.queued) == 1
    session.commit()
    return session.get(ArchiveJob, queued.queue_result.queued[0])


def _retry_now(session, job):
    retried = retry_due_archive_jobs(
        session,
        now=datetime.now(timezone.utc) + timedelta(seconds=30),
        base_delay_seconds=1,
        max_delay_seconds=1,
    )
    assert retried == 1
    session.commit()
    session.refresh(job)
    assert job.state == "queued"


def _assert_one_verified_asset(session, source_id, volume_root, payload):
    assets = list(session.scalars(
        select(LocalAsset).where(LocalAsset.source_recording_id == source_id)
    ))
    assert len(assets) == 1
    asset = assets[0]
    assert (asset.verification_state, asset.lifecycle_state) == ("verified", "present")
    assert asset.sha256 == hashlib.sha256(payload).hexdigest()
    assert (volume_root / asset.relative_path).read_bytes() == payload
    assert session.query(ArchiveJob).filter_by(source_recording_id=source_id).count() == 1


def _client(nvr: MockHikvisionNvr, *, timeout_seconds: float = 5):
    return HikvisionIsapiClient(
        base_url=nvr.base_url, username=nvr.username, password=nvr.password,
        timeout_seconds=timeout_seconds, download_method="POST",
    )


def test_s011_search_503_recovers_after_stable_rescans_without_duplicate(session, tmp_path, monkeypatch):
    payload = _synthetic_video(tmp_path / "s011-search-503.mp4")
    nvr, camera, _volume, volume_root, recording, now = _setup(session, tmp_path, "search-503")
    recording = MockRecording(recording.track_id, recording.start, recording.end, payload)
    with MockHikvisionNvr([recording], search_statuses=[503]) as mock_nvr:
        with _client(mock_nvr) as client:
            failed = scan_camera_range(
                session, nvr=nvr, camera=camera,
                query_start=recording.start - timedelta(days=1),
                query_end=recording.end + timedelta(days=1), searcher=client,
                scan_id="s011-search-503-first", scan_min_date=now.date() - timedelta(days=5),
            )
            session.commit()
            assert (failed.state, failed.error_code) == ("failed", "NVR_SEARCH_HTTP_503")
            assert session.scalar(select(SourceRecording).where(
                SourceRecording.nvr_id == nvr.id
            )) is None
            job = _discover_and_queue(session, nvr, camera, now, client, recording)
            monkeypatch.setattr(archive_worker, "_touch_heartbeat", lambda *_a, **_k: None)
            assert archive_worker.process_one(
                session, client, probe=FfprobeMediaProbe(), worker_id="s011-search-503"
            ) == job.id

    source = session.scalar(select(SourceRecording).where(SourceRecording.nvr_id == nvr.id))
    assert source is not None
    assert mock_nvr.search_count == 4
    assert mock_nvr.download_count == 1
    _assert_one_verified_asset(session, source.id, volume_root, payload)


def test_s011_download_timeout_retries_once_without_duplicate_asset(session, tmp_path, monkeypatch):
    payload = _synthetic_video(tmp_path / "s011-download-timeout.mp4")
    nvr, camera, _volume, volume_root, recording, now = _setup(session, tmp_path, "download-timeout")
    recording = MockRecording(recording.track_id, recording.start, recording.end, payload)
    with MockHikvisionNvr([recording], download_delays=[0.75]) as mock_nvr:
        with _client(mock_nvr, timeout_seconds=0.25) as client:
            job = _discover_and_queue(session, nvr, camera, now, client, recording)
            monkeypatch.setattr(archive_worker, "_touch_heartbeat", lambda *_a, **_k: None)
            assert archive_worker.process_one(
                session, client, probe=FfprobeMediaProbe(), worker_id="s011-download-timeout"
            ) == job.id
            session.refresh(job)
            assert (job.state, job.error_code) == ("failed", "NVR_DOWNLOAD_FAILED")
            failed_attempt_count = job.attempt_count
            _retry_now(session, job)
            assert job.attempt_count == failed_attempt_count
            assert archive_worker.process_one(
                session, client, probe=FfprobeMediaProbe(), worker_id="s011-download-timeout"
            ) == job.id

    session.refresh(job)
    assert job.state == "completed"
    assert mock_nvr.download_count == 2
    _assert_one_verified_asset(session, job.source_recording_id, volume_root, payload)


def test_s011_offline_search_peer_retries_with_online_peer_without_duplicate(session, tmp_path, monkeypatch):
    payload = _synthetic_video(tmp_path / "s011-offline-peer.mp4")
    nvr, camera, _volume, volume_root, recording, now = _setup(session, tmp_path, "offline-peer")
    recording = MockRecording(recording.track_id, recording.start, recording.end, payload)
    with MockHikvisionNvr([recording]) as mock_nvr:
        with _client(mock_nvr) as online_client:
            job = _discover_and_queue(session, nvr, camera, now, online_client, recording)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                closed_port = sock.getsockname()[1]
            with HikvisionIsapiClient(
                base_url=f"http://127.0.0.1:{closed_port}", username="fixture-user",
                password="fixture-password", timeout_seconds=1, download_method="POST",
            ) as offline_client:
                monkeypatch.setattr(archive_worker, "_touch_heartbeat", lambda *_a, **_k: None)
                assert archive_worker.process_one(
                    session, offline_client, probe=FfprobeMediaProbe(), worker_id="s011-offline"
                ) == job.id
                session.refresh(job)
                assert (job.state, job.error_code) == (
                    "failed", "NVR_SEARCH_TRANSPORT_FAILED"
                )
            _retry_now(session, job)
            assert archive_worker.process_one(
                session, online_client, probe=FfprobeMediaProbe(), worker_id="s011-online"
            ) == job.id

    session.refresh(job)
    assert job.state == "completed"
    assert mock_nvr.download_count == 1
    _assert_one_verified_asset(session, job.source_recording_id, volume_root, payload)
