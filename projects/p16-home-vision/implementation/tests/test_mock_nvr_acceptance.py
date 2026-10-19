from __future__ import annotations

import hashlib
import shutil
import subprocess
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.domain.archive_pipeline import ArchiveValidationError, verify_part
from app.domain.archive_queue import retry_due_archive_jobs
from app.domain.auto_archive import enqueue_closed_day_backlog
from app.domain.nvr_scan import scan_camera_range
from app.infra.media.ffprobe import FfprobeMediaProbe
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
