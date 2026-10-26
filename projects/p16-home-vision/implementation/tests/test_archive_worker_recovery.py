import hashlib
import multiprocessing
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from app.db import create_db_engine
from app.domain import archive_pipeline
from app.infra.media.ffprobe import MediaProbeResult
from app.infra.nvr.hikvision import DiscoveredSegment
from app.models import AuditEvent, ArchiveJob, CameraTrack, LocalAsset, NvrDevice, SourceRecording, StorageVolume
from scripts import archive_worker


class FakeProbe:
    def probe(self, path: Path) -> MediaProbeResult:
        return MediaProbeResult(
            duration_ms=10_000, has_video=True, container="mp4",
            video_codec="h264", audio_codec="aac", width=1920, height=1080,
        )


class NoNetworkClient:
    def search_history(self, _request):
        raise AssertionError("orphan recovery must not query NVR")

    def download_to_part(self, *_args, **_kwargs):
        raise AssertionError("orphan recovery must not redownload source media")


class _S007SyntheticClient:
    def __init__(self):
        self.segment = DiscoveredSegment(
            track_id="201", source_start=datetime(2026, 8, 27, 6, tzinfo=timezone.utc),
            source_end=datetime(2026, 8, 27, 6, 0, 10, tzinfo=timezone.utc),
            duration_ms=10_000, locator_fingerprint="s007-synthetic",
            playback_uri="synthetic://s007", source_size_if_known=4096,
        )

    def search_history(self, _request):
        return [self.segment]

    def download_to_part(self, _uri, part_path, *, progress_callback=None):
        payload = b"VIDEO" + b"x" * 4091
        part_path.parent.mkdir(parents=True, exist_ok=True)
        part_path.write_bytes(payload)
        if progress_callback:
            progress_callback(len(payload))
        return len(payload)


class _S009BlockingClient(_S007SyntheticClient):
    def __init__(self, partial_pipe):
        super().__init__()
        self.partial_pipe = partial_pipe

    def download_to_part(self, _uri, part_path, *, progress_callback=None):
        payload = b"PARTIAL-S009-RECOVERY-FIXTURE"
        part_path.parent.mkdir(parents=True, exist_ok=True)
        part_path.write_bytes(payload)
        if progress_callback:
            progress_callback(len(payload))
        self.partial_pipe.send(str(part_path))
        while True:
            time.sleep(10)


def _s007_kill_after_promotion(database_url, promoted_pipe):
    engine = create_db_engine(database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    original_replace = archive_pipeline.os.replace

    def promote_then_pause(source, destination):
        original_replace(source, destination)
        if Path(source).suffix == ".part" and Path(destination).suffix == ".mp4":
            promoted_pipe.send((str(destination), hashlib.sha256(Path(destination).read_bytes()).hexdigest()))
            while True:
                time.sleep(10)

    archive_pipeline.os.replace = promote_then_pause
    archive_worker._touch_heartbeat = lambda *_args, **_kwargs: None
    with factory() as child_session:
        archive_worker.process_one(
            child_session, _S007SyntheticClient(), probe=FakeProbe(), worker_id="s007-kill-worker"
        )
    engine.dispose()


def _s009_kill_during_download(database_url, partial_pipe):
    engine = create_db_engine(database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    archive_worker._touch_heartbeat = lambda *_args, **_kwargs: None
    with factory() as child_session:
        archive_worker.process_one(
            child_session, _S009BlockingClient(partial_pipe),
            probe=FakeProbe(), worker_id="s009-kill-worker",
        )
    engine.dispose()


def test_worker_adopts_valid_promoted_orphan_before_nvr_access(session, tmp_path, monkeypatch):
    nvr = NvrDevice(id="nvr-orphan", name="NVR", host="fixture")
    camera = CameraTrack(id="camera-orphan", nvr_id=nvr.id, track_id="201", camera_name="Cam")
    source = SourceRecording(
        id="source-orphan", nvr_id=nvr.id, track_id=camera.track_id,
        source_start=datetime(2026, 8, 27, 6, tzinfo=timezone.utc),
        source_end=datetime(2026, 8, 27, 6, 0, 10, tzinfo=timezone.utc),
        duration_ms=10_000, source_size_if_known=4096, source_state="available",
    )
    volume = StorageVolume(
        id="volume-orphan", node_id="fixture", mount_path_runtime=str(tmp_path),
        total_bytes=2 * 1024**4, free_bytes=2 * 1024**4, online=True, writable=True,
    )
    job = ArchiveJob(id="job-orphan", source_recording_id=source.id, state="queued")
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.flush()
    session.add(source)
    session.flush()
    session.add(volume)
    session.flush()
    session.add(job)
    session.commit()

    final_path = (
        tmp_path / "recordings/2026/08/27/camera-201/060000-source-orphan.mp4"
    )
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"VIDEO" + b"x" * 4091)
    monkeypatch.setattr(archive_worker, "_touch_heartbeat", lambda *_args, **_kwargs: None)

    assert archive_worker.process_one(
        session, NoNetworkClient(), probe=FakeProbe(), worker_id="fixture-worker"
    ) == job.id
    assert session.get(ArchiveJob, job.id).state == "completed"
    assert session.query(LocalAsset).filter_by(source_recording_id=source.id).count() == 1
    assert final_path.exists()


def test_s008_cross_volume_orphan_is_adopted_without_duplicate_or_download(
    session, tmp_path, monkeypatch
):
    nvr = NvrDevice(id="nvr-s008", name="S008 synthetic", host="127.0.0.1")
    camera = CameraTrack(id="camera-s008", nvr_id=nvr.id, track_id="202",
                         camera_name="S008 synthetic")
    source = SourceRecording(
        id="source-s008", nvr_id=nvr.id, track_id=camera.track_id,
        source_start=datetime(2026, 8, 27, 6, tzinfo=timezone.utc),
        source_end=datetime(2026, 8, 27, 6, 0, 10, tzinfo=timezone.utc),
        duration_ms=10_000, source_size_if_known=4096, source_state="available",
    )
    volume_a_root = tmp_path / "volume-a"
    volume_b_root = tmp_path / "volume-b"
    volume_a = StorageVolume(
        id="volume-s008-a", node_id="fixture", mount_path_runtime=str(volume_a_root),
        total_bytes=1024**4, free_bytes=512 * 1024**3, online=True, writable=False,
    )
    volume_b = StorageVolume(
        id="volume-s008-b", node_id="fixture", mount_path_runtime=str(volume_b_root),
        total_bytes=2 * 1024**4, free_bytes=2 * 1024**4, online=True, writable=True,
    )
    job = ArchiveJob(id="job-s008", source_recording_id=source.id, state="queued")
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.flush()
    session.add(source)
    session.add_all([volume_a, volume_b])
    session.flush()
    session.add(job)
    session.commit()

    relative_path = Path(
        "recordings/2026/08/27/camera-202/060000-source-s008.mp4"
    )
    orphan = volume_a_root / relative_path
    selected_volume_target = volume_b_root / relative_path
    orphan.parent.mkdir(parents=True)
    volume_b_root.mkdir(parents=True)
    original_bytes = b"VIDEO" + b"x" * 4091
    orphan.write_bytes(original_bytes)
    orphan_sha = hashlib.sha256(original_bytes).hexdigest()
    monkeypatch.setattr(archive_worker, "_touch_heartbeat", lambda *_args, **_kwargs: None)

    assert archive_worker.process_one(
        session, NoNetworkClient(), probe=FakeProbe(), worker_id="s008-worker"
    ) == job.id

    asset = session.query(LocalAsset).filter_by(source_recording_id=source.id).one()
    recovered = session.get(ArchiveJob, job.id)
    assert recovered.state == "completed"
    assert (asset.storage_volume_id, asset.relative_path) == (volume_a.id, str(relative_path))
    assert asset.sha256 == orphan_sha
    assert orphan.read_bytes() == original_bytes
    assert not selected_volume_target.exists()
    assert session.query(LocalAsset).filter_by(source_recording_id=source.id).count() == 1
    assert session.query(AuditEvent).filter_by(
        event_type="archive_orphan_recovered", entity_id=asset.id
    ).count() == 1


def test_s007_sigkill_after_file_promotion_recovers_without_nvr_redownload(session, tmp_path):
    nvr = NvrDevice(id="nvr-s007-kill", name="S007 synthetic", host="127.0.0.1")
    camera = CameraTrack(id="camera-s007-kill", nvr_id=nvr.id, track_id="201",
                         camera_name="S007 synthetic")
    source = SourceRecording(
        id="source-s007-kill", nvr_id=nvr.id, track_id=camera.track_id,
        source_start=datetime(2026, 8, 27, 6, tzinfo=timezone.utc),
        source_end=datetime(2026, 8, 27, 6, 0, 10, tzinfo=timezone.utc),
        duration_ms=10_000, source_size_if_known=4096, source_state="available",
    )
    volume_root = tmp_path / "s007-volume"
    volume = StorageVolume(
        id="volume-s007-kill", node_id="fixture", mount_path_runtime=str(volume_root),
        total_bytes=2 * 1024**4, free_bytes=2 * 1024**4, online=True, writable=True,
    )
    job = ArchiveJob(id="job-s007-kill", source_recording_id=source.id, state="queued")
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.flush()
    session.add(source)
    session.add(volume)
    session.flush()
    session.add(job)
    session.commit()

    context = multiprocessing.get_context("fork")
    recv_pipe, send_pipe = context.Pipe(duplex=False)
    child = context.Process(
        target=_s007_kill_after_promotion,
        args=(session.get_bind().url.render_as_string(hide_password=False), send_pipe),
    )
    child.start()
    send_pipe.close()
    try:
        assert recv_pipe.poll(20), "worker never reached the post-promotion crash point"
        promoted_path, promoted_hash = recv_pipe.recv()
        os.kill(child.pid, signal.SIGKILL)
        child.join(timeout=10)
        assert child.exitcode == -signal.SIGKILL

        final_path = Path(promoted_path)
        assert final_path.is_file()
        assert hashlib.sha256(final_path.read_bytes()).hexdigest() == promoted_hash
        session.expire_all()
        assert session.query(LocalAsset).filter_by(source_recording_id=source.id).count() == 0
        interrupted = session.get(ArchiveJob, job.id)
        assert interrupted.state == "verifying"

        interrupted.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
        archive_worker._recover_interrupted_jobs(session)
        assert session.get(ArchiveJob, job.id).state == "queued"
        archive_worker._touch_heartbeat = lambda *_args, **_kwargs: None
        assert archive_worker.process_one(
            session, NoNetworkClient(), probe=FakeProbe(), worker_id="s007-recovery-worker"
        ) == job.id

        asset = session.query(LocalAsset).filter_by(source_recording_id=source.id).one()
        assert session.get(ArchiveJob, job.id).state == "completed"
        assert asset.verification_state == "verified"
        assert asset.sha256 == promoted_hash
        assert hashlib.sha256(final_path.read_bytes()).hexdigest() == promoted_hash
        assert session.query(AuditEvent).filter_by(
            event_type="archive_orphan_recovered", entity_id=asset.id
        ).count() == 1
    finally:
        if child.is_alive():
            os.kill(child.pid, signal.SIGKILL)
            child.join(timeout=10)
        recv_pipe.close()


def test_s009_killed_download_recovers_quarantines_part_and_completes_once(session, tmp_path):
    nvr = NvrDevice(id="nvr-s009-kill", name="S009 synthetic", host="127.0.0.1")
    camera = CameraTrack(id="camera-s009-kill", nvr_id=nvr.id, track_id="201",
                         camera_name="S009 synthetic")
    source = SourceRecording(
        id="source-s009-kill", nvr_id=nvr.id, track_id=camera.track_id,
        source_start=datetime(2026, 8, 27, 6, tzinfo=timezone.utc),
        source_end=datetime(2026, 8, 27, 6, 0, 10, tzinfo=timezone.utc),
        duration_ms=10_000, source_size_if_known=4096, source_state="available",
    )
    volume_root = tmp_path / "s009-volume"
    volume = StorageVolume(
        id="volume-s009-kill", node_id="fixture", mount_path_runtime=str(volume_root),
        total_bytes=2 * 1024**4, free_bytes=2 * 1024**4, online=True, writable=True,
    )
    job = ArchiveJob(id="job-s009-kill", source_recording_id=source.id, state="queued")
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.flush()
    session.add(source)
    session.add(volume)
    session.flush()
    session.add(job)
    session.commit()

    context = multiprocessing.get_context("fork")
    recv_pipe, send_pipe = context.Pipe(duplex=False)
    child = context.Process(
        target=_s009_kill_during_download,
        args=(session.get_bind().url.render_as_string(hide_password=False), send_pipe),
    )
    child.start()
    send_pipe.close()
    try:
        assert recv_pipe.poll(20), "worker never reached the partial-download crash point"
        partial_path = Path(recv_pipe.recv())
        os.kill(child.pid, signal.SIGKILL)
        child.join(timeout=10)
        assert child.exitcode == -signal.SIGKILL
        assert partial_path.read_bytes() == b"PARTIAL-S009-RECOVERY-FIXTURE"

        session.expire_all()
        interrupted = session.get(ArchiveJob, job.id)
        assert interrupted.state == "downloading"
        assert interrupted.bytes_received == len(b"PARTIAL-S009-RECOVERY-FIXTURE")
        interrupted.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
        archive_worker._recover_interrupted_jobs(session)
        assert session.get(ArchiveJob, job.id).state == "queued"

        archive_worker._touch_heartbeat = lambda *_args, **_kwargs: None
        assert archive_worker.process_one(
            session, _S007SyntheticClient(), probe=FakeProbe(), worker_id="s009-restart-worker"
        ) == job.id
        asset = session.query(LocalAsset).filter_by(source_recording_id=source.id).one()
        final_path = volume_root / "recordings/2026/08/27/camera-201/060000-source-s009-kill.mp4"
        quarantined = list((volume_root / ".quarantine").glob("*.partial"))
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == b"PARTIAL-S009-RECOVERY-FIXTURE"
        assert session.query(AuditEvent).filter_by(
            event_type="archive_partial_quarantined", entity_id=job.id
        ).count() == 1
        assert final_path.is_file()
        assert asset.verification_state == "verified"
        assert asset.sha256 == hashlib.sha256(final_path.read_bytes()).hexdigest()
        assert session.query(LocalAsset).filter_by(source_recording_id=source.id).count() == 1
        recovered = session.get(ArchiveJob, job.id)
        assert recovered.state == "completed"
        assert recovered.attempt_count == 2
    finally:
        if child.is_alive():
            os.kill(child.pid, signal.SIGKILL)
            child.join(timeout=10)
        recv_pipe.close()


import pytest


@pytest.mark.parametrize(
    "volume_state",
    [
        {"total_bytes": 2 * 1024**4, "free_bytes": 0, "online": True, "writable": True},
        {"total_bytes": 2 * 1024**4, "free_bytes": 2 * 1024**4, "online": False, "writable": True},
        {"total_bytes": 2 * 1024**4, "free_bytes": 2 * 1024**4, "online": True, "writable": False},
    ],
    ids=["zero-space", "offline", "permission-denied"],
)
def test_s013_storage_faults_fail_closed_without_touching_siblings(
    session, tmp_path, monkeypatch, volume_state
):
    nvr = NvrDevice(id="nvr-s013", name="S013 synthetic", host="127.0.0.1")
    camera = CameraTrack(id="camera-s013", nvr_id=nvr.id, track_id="213",
                         camera_name="S013 synthetic")
    source = SourceRecording(
        id="source-s013", nvr_id=nvr.id, track_id=camera.track_id,
        source_start=datetime(2026, 8, 27, 6, tzinfo=timezone.utc),
        source_end=datetime(2026, 8, 27, 6, 0, 10, tzinfo=timezone.utc),
        duration_ms=10_000, source_size_if_known=4096, source_state="available",
    )
    volume_root = tmp_path / "volume"
    volume_root.mkdir()
    volume = StorageVolume(
        id="volume-s013", node_id="fixture", mount_path_runtime=str(volume_root),
        **volume_state,
    )
    job = ArchiveJob(id="job-s013", source_recording_id=source.id, state="queued")
    sibling = volume_root / "unrelated-sentinel.bin"
    sibling.write_bytes(b"preserve-exact-sibling-bytes")
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.flush()
    session.add(source)
    session.flush()
    session.add(volume)
    session.flush()
    session.add(job)
    session.commit()
    monkeypatch.setattr(archive_worker, "_touch_heartbeat", lambda *_args, **_kwargs: None)

    assert archive_worker.process_one(
        session, NoNetworkClient(), probe=FakeProbe(), worker_id="s013-fixture"
    ) == job.id
    failed = session.get(ArchiveJob, job.id)
    assert failed.state == "failed"
    assert failed.error_code == "STORAGE_NOT_READY"
    assert session.query(LocalAsset).filter_by(source_recording_id=source.id).count() == 0
    audits = session.query(AuditEvent).filter_by(
        event_type="archive_attempt_failed", entity_id=job.id
    ).all()
    assert len(audits) == 1
    assert '"error_code": "STORAGE_NOT_READY"' in audits[0].payload_redacted
    assert sibling.read_bytes() == b"preserve-exact-sibling-bytes"
    assert sorted(p.name for p in volume_root.iterdir()) == ["unrelated-sentinel.bin"]


def test_s013_write_time_eacces_after_preflight_fails_closed(session, tmp_path, monkeypatch):
    """Inject a real filesystem EACCES after the volume metadata preflight passes."""
    nvr = NvrDevice(id="nvr-s013-eacces", name="S013 synthetic", host="127.0.0.1")
    camera = CameraTrack(id="camera-s013-eacces", nvr_id=nvr.id, track_id="214",
                         camera_name="S013 synthetic")
    source_start = datetime(2026, 8, 27, 7, tzinfo=timezone.utc)
    source = SourceRecording(
        id="source-s013-eacces", nvr_id=nvr.id, track_id=camera.track_id,
        source_start=source_start, source_end=source_start + timedelta(seconds=10),
        duration_ms=10_000, source_size_if_known=4096, source_state="available",
    )
    volume_root = tmp_path / "volume-eacces"
    destination = volume_root / "recordings" / "2026/08/27" / "camera-214"
    destination.mkdir(parents=True)
    sentinel = volume_root / "unrelated-sentinel.bin"
    sentinel.write_bytes(b"preserve-exact-sibling-bytes")
    volume = StorageVolume(
        id="volume-s013-eacces", node_id="fixture", mount_path_runtime=str(volume_root),
        total_bytes=2 * 1024**4, free_bytes=2 * 1024**4, online=True, writable=True,
    )
    job = ArchiveJob(id="job-s013-eacces", source_recording_id=source.id, state="queued")
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.flush()
    session.add(source)
    session.flush()
    session.add(volume)
    session.flush()
    session.add(job)
    session.commit()

    segment = DiscoveredSegment(
        track_id=camera.track_id, source_start=source_start,
        source_end=source_start + timedelta(seconds=10), duration_ms=10_000,
        locator_fingerprint="s013-eacces-synthetic", playback_uri="synthetic://s013-eacces",
        source_size_if_known=4096,
    )

    class Client:
        def download_to_part(self, _uri, path, *, progress_callback=None):
            path.write_bytes(b"x" * 4096)
            if progress_callback:
                progress_callback(4096)
            return 4096

    original_mkdir = Path.mkdir

    def deny_final_directory(path, *args, **kwargs):
        if path == destination:
            raise PermissionError(13, "synthetic EACCES", str(path))
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(archive_worker, "_touch_heartbeat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(archive_worker, "_find_exact_segment", lambda *_args, **_kwargs: segment)
    monkeypatch.setattr(Path, "mkdir", deny_final_directory)
    result = archive_worker.process_one(
        session, Client(), probe=FakeProbe(), worker_id="s013-eacces-worker"
    )

    assert result == job.id
    failed = session.get(ArchiveJob, job.id)
    assert failed.state == "failed"
    assert failed.error_code == "ARCHIVE_WORKER_FAILED"
    assert session.query(LocalAsset).filter_by(source_recording_id=source.id).count() == 0
    assert sentinel.read_bytes() == b"preserve-exact-sibling-bytes"
    assert not any(path.name.endswith(".mp4") for path in volume_root.rglob("*"))
