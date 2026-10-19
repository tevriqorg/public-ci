from datetime import datetime
import hashlib
from pathlib import Path

import pytest

from app.domain.archive_pipeline import (
    ArchiveValidationError,
    RetryPolicy,
    VerificationPolicy,
    finalize_archive_job,
    quarantine_partial_file,
    verify_part,
)
from app.infra.media.ffprobe import MediaProbeResult
from app.models import ArchiveJob, AuditEvent, CameraTrack, LocalAsset, NvrDevice, SourceRecording, StorageVolume


class FakeProbe:
    def __init__(self, *, duration_ms=10000, has_video=True):
        self.result = MediaProbeResult(
            duration_ms=duration_ms,
            has_video=has_video,
            container="mpegts",
            video_codec="hevc",
            audio_codec="pcm_alaw",
            width=2560,
            height=1440,
        )

    def probe(self, _path: Path) -> MediaProbeResult:
        return self.result


def write_bytes(path: Path, prefix: bytes = b"VIDEO", size: int = 4096) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = prefix + b"x" * max(0, size - len(prefix))
    path.write_bytes(body)


def test_verify_rejects_xml_error_payload(tmp_path) -> None:
    part = tmp_path / "x.part"
    write_bytes(part, b"<?xml version='1.0'?><ResponseStatus>", 4096)
    with pytest.raises(ArchiveValidationError, match="DOWNLOAD_ERROR_PAYLOAD"):
        verify_part(part, expected_duration_ms=10000, probe=FakeProbe())


def test_verify_rejects_too_small_file(tmp_path) -> None:
    part = tmp_path / "x.part"
    write_bytes(part, b"V", 10)
    with pytest.raises(ArchiveValidationError, match="DOWNLOAD_TOO_SMALL"):
        verify_part(part, expected_duration_ms=10000, probe=FakeProbe())


def test_verify_rejects_no_video_and_duration_mismatch(tmp_path) -> None:
    part = tmp_path / "x.part"
    write_bytes(part)
    with pytest.raises(ArchiveValidationError, match="VIDEO_STREAM_MISSING"):
        verify_part(part, expected_duration_ms=10000, probe=FakeProbe(has_video=False))
    with pytest.raises(ArchiveValidationError, match="MEDIA_DURATION_MISMATCH"):
        verify_part(
            part,
            expected_duration_ms=10000,
            probe=FakeProbe(duration_ms=20000),
            policy=VerificationPolicy(duration_tolerance_ms=1000),
        )


def test_verify_accepts_nvr_timestamp_drift_when_size_matches(tmp_path) -> None:
    part = tmp_path / "x.part"
    write_bytes(part, size=4096)
    digest, media, size = verify_part(
        part,
        expected_duration_ms=10000,
        expected_size_bytes=4096,
        probe=FakeProbe(duration_ms=25000),
    )
    assert len(digest) == 64
    assert media.duration_ms == 25000
    assert size == 4096


def test_verify_accepts_small_nvr_size_drift_with_timestamp_drift(tmp_path) -> None:
    part = tmp_path / "x.part"
    write_bytes(part, size=4136)
    digest, media, size = verify_part(
        part,
        expected_duration_ms=10000,
        expected_size_bytes=4096,
        probe=FakeProbe(duration_ms=25000),
    )
    assert len(digest) == 64
    assert media.duration_ms == 25000
    assert size == 4136


def test_verify_accepts_large_timestamp_drift_when_nvr_size_matches(tmp_path) -> None:
    part = tmp_path / "x.part"
    write_bytes(part, size=4096)
    digest, media, size = verify_part(
        part,
        expected_duration_ms=10000,
        expected_size_bytes=4096,
        probe=FakeProbe(duration_ms=98000),
    )
    assert len(digest) == 64
    assert media.duration_ms == 98000
    assert size == 4096


def test_verify_rejects_large_timestamp_drift_without_matching_nvr_size(tmp_path) -> None:
    part = tmp_path / "x.part"
    write_bytes(part, size=4096)
    with pytest.raises(ArchiveValidationError, match="MEDIA_DURATION_MISMATCH"):
        verify_part(
            part,
            expected_duration_ms=10000,
            expected_size_bytes=8193,
            probe=FakeProbe(duration_ms=50001),
        )


def test_verify_accepts_missing_duration_when_nvr_size_matches(tmp_path) -> None:
    part = tmp_path / "x.part"
    write_bytes(part, size=4096)
    digest, media, size = verify_part(
        part,
        expected_duration_ms=10000,
        expected_size_bytes=4096,
        probe=FakeProbe(duration_ms=0),
    )
    assert len(digest) == 64
    assert media.duration_ms == 0
    assert size == 4096


def test_finalize_promotes_only_after_verification(session, tmp_path) -> None:
    nvr = NvrDevice(id="nvr", name="NVR", host="fixture")
    session.add(nvr)
    session.flush()
    track = CameraTrack(id="cam", nvr_id="nvr", track_id="201", camera_name="Cam")
    session.add(track)
    session.flush()
    source = SourceRecording(
        id="src",
        nvr_id="nvr",
        track_id="201",
        source_start=datetime(2026, 8, 27, 6),
        source_end=datetime(2026, 8, 27, 6, 0, 10),
        duration_ms=10000,
    )
    volume = StorageVolume(
        id="archive-a", node_id="fixture", mount_path_runtime=str(tmp_path), online=True, writable=True
    )
    session.add_all([source, volume])
    session.flush()
    job = ArchiveJob(id="job", source_recording_id=source.id, state="queued")
    session.add(job)
    session.flush()
    part = tmp_path / "incoming" / "job.part"
    final = tmp_path / "archive" / "video.mp4"
    write_bytes(part, size=8192)

    asset = finalize_archive_job(
        session,
        job=job,
        source=source,
        storage_volume_id=volume.id,
        relative_path="archive/video.mp4",
        part_path=part,
        final_path=final,
        probe=FakeProbe(),
        asset_id="asset",
    )
    assert job.state == "completed"
    assert asset.verification_state == "verified"
    assert asset.archive_source_snapshot_id is not None
    assert len(asset.sha256 or "") == 64
    assert final.exists() and not part.exists()


def test_finalize_recovers_valid_promoted_file_after_database_commit_gap(session, tmp_path) -> None:
    nvr = NvrDevice(id="nvr-repair", name="NVR", host="fixture")
    session.add(nvr)
    session.flush()
    session.add(CameraTrack(id="cam-repair", nvr_id=nvr.id, track_id="201", camera_name="Cam"))
    session.flush()
    source = SourceRecording(
        id="src-repair", nvr_id=nvr.id, track_id="201",
        source_start=datetime(2026, 8, 27, 6),
        source_end=datetime(2026, 8, 27, 6, 0, 10), duration_ms=10_000,
    )
    volume = StorageVolume(
        id="archive-repair", node_id="fixture", mount_path_runtime=str(tmp_path), online=True, writable=True
    )
    job = ArchiveJob(id="job-repair", source_recording_id=source.id, state="queued")
    session.add(source)
    session.flush()
    session.add_all([volume, job])
    session.flush()
    final = tmp_path / "archive" / "video.mp4"
    part = tmp_path / "archive" / "video.mp4.part"
    write_bytes(part, size=8192)
    job.state = "verifying"
    session.commit()

    finalize_archive_job(
        session, job=job, source=source, storage_volume_id=volume.id,
        relative_path="archive/video.mp4", part_path=part, final_path=final,
        storage_root=tmp_path, probe=FakeProbe(), asset_id="asset-before-crash",
    )
    assert final.exists() and not part.exists()
    session.rollback()  # injected crash after os.replace and before DB commit
    assert session.query(LocalAsset).count() == 0
    before_hash = hashlib.sha256(final.read_bytes()).hexdigest()

    job = session.get(ArchiveJob, "job-repair")
    source = session.get(SourceRecording, "src-repair")
    volume = session.get(StorageVolume, "archive-repair")

    asset = finalize_archive_job(
        session, job=job, source=source, storage_volume_id=volume.id,
        relative_path="archive/video.mp4", part_path=tmp_path / "missing.part",
        final_path=final, storage_root=tmp_path, probe=FakeProbe(), asset_id="asset-repair",
    )
    session.flush()

    assert job.state == "completed"
    assert asset.verification_state == "verified"
    assert final.exists()
    assert hashlib.sha256(final.read_bytes()).hexdigest() == before_hash
    assert session.query(AuditEvent).filter_by(event_type="archive_orphan_recovered").count() == 1


def test_invalid_promoted_orphan_is_quarantined_without_deletion(session, tmp_path) -> None:
    nvr = NvrDevice(id="nvr-quarantine", name="NVR", host="fixture")
    session.add(nvr)
    session.flush()
    session.add(CameraTrack(id="cam-quarantine", nvr_id=nvr.id, track_id="201", camera_name="Cam"))
    session.flush()
    source = SourceRecording(
        id="src-quarantine", nvr_id=nvr.id, track_id="201",
        source_start=datetime(2026, 8, 27, 6),
        source_end=datetime(2026, 8, 27, 6, 0, 10), duration_ms=10_000,
    )
    volume = StorageVolume(
        id="archive-quarantine", node_id="fixture", mount_path_runtime=str(tmp_path), online=True, writable=True
    )
    job = ArchiveJob(id="job-quarantine", source_recording_id=source.id, state="queued")
    session.add(source)
    session.flush()
    session.add_all([volume, job])
    session.flush()
    final = tmp_path / "recordings" / "video.mp4"
    write_bytes(final, b"<html>bad payload", 4096)

    with pytest.raises(ArchiveValidationError, match="ORPHAN_FINAL_QUARANTINED"):
        finalize_archive_job(
            session, job=job, source=source, storage_volume_id=volume.id,
            relative_path="recordings/video.mp4", part_path=tmp_path / "missing.part",
            final_path=final, storage_root=tmp_path, probe=FakeProbe(), asset_id="asset-quarantine",
        )

    quarantined = list((final.parent / ".quarantine").glob("*.invalid"))
    assert not final.exists()
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes().startswith(b"<html>")
    assert session.query(AuditEvent).filter_by(event_type="archive_orphan_quarantined").count() == 1


def test_stale_partial_is_quarantined_and_audited(session, tmp_path) -> None:
    partial = tmp_path / "recordings" / "camera-1" / "video.mp4.part"
    write_bytes(partial, size=2048)
    target = quarantine_partial_file(
        session,
        path=partial,
        storage_root=tmp_path,
        job_id="job-partial",
    )
    session.flush()
    assert target is not None
    assert not partial.exists()
    assert target.read_bytes().startswith(b"VIDEO")
    assert session.query(AuditEvent).filter_by(event_type="archive_partial_quarantined").count() == 1


def test_failed_verification_never_completes_job(session, tmp_path) -> None:
    nvr = NvrDevice(id="nvr2", name="NVR", host="fixture")
    session.add(nvr)
    session.flush()
    track = CameraTrack(id="cam2", nvr_id="nvr2", track_id="201", camera_name="Cam")
    session.add(track)
    session.flush()
    source = SourceRecording(
        id="src2",
        nvr_id="nvr2",
        track_id="201",
        source_start=datetime(2026, 8, 27, 6),
        source_end=datetime(2026, 8, 27, 6, 0, 10),
        duration_ms=10000,
    )
    volume = StorageVolume(
        id="archive-b", node_id="fixture", mount_path_runtime=str(tmp_path), online=True, writable=True
    )
    session.add_all([source, volume])
    session.flush()
    job = ArchiveJob(id="job2", source_recording_id=source.id, state="queued")
    session.add(job)
    session.flush()
    part = tmp_path / "job2.part"
    final = tmp_path / "final.mp4"
    write_bytes(part, b"<html>error", 4096)

    with pytest.raises(ArchiveValidationError):
        finalize_archive_job(
            session,
            job=job,
            source=source,
            storage_volume_id=volume.id,
            relative_path="final.mp4",
            part_path=part,
            final_path=final,
            probe=FakeProbe(),
            asset_id="asset2",
        )
    assert job.state == "failed"
    assert not final.exists()


def test_retry_policy_is_bounded() -> None:
    policy = RetryPolicy(max_attempts=3, base_delay_seconds=1, max_delay_seconds=10)
    assert policy.delay_for_attempt(1) == 1
    assert policy.delay_for_attempt(2) == 2
    assert policy.delay_for_attempt(3) is None
