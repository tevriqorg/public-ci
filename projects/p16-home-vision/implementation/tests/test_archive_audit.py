from datetime import datetime, timedelta, timezone

from app.domain.archive_audit import audit_archive_state
from app.models import ArchiveJob, CameraTrack, LocalAsset, NvrDevice, SourceRecording, StorageVolume


def test_read_only_archive_audit_reports_db_only_orphans_partials_and_stale_lease(session, tmp_path) -> None:
    volume_root = tmp_path / "volume"
    recordings = volume_root / "recordings" / "2026" / "10" / "18"
    recordings.mkdir(parents=True)
    (recordings / "orphan.mp4").write_bytes(b"fixture-orphan")
    (recordings / "download.mp4.part").write_bytes(b"fixture-partial")
    quarantine = volume_root / "recordings" / ".quarantine"
    quarantine.mkdir(parents=True)
    (quarantine / "orphan.mp4.invalid").write_bytes(b"fixture-quarantined")

    nvr = NvrDevice(id="nvr-audit", name="NVR", host="fixture")
    camera = CameraTrack(id="camera-audit", nvr_id=nvr.id, track_id="101", camera_name="Camera")
    source = SourceRecording(
        id="source-audit", nvr_id=nvr.id, track_id=camera.track_id,
        source_start=datetime(2026, 10, 18, tzinfo=timezone.utc),
        source_end=datetime(2026, 10, 18, 0, 0, 10, tzinfo=timezone.utc),
        duration_ms=10_000, source_state="available",
    )
    volume = StorageVolume(
        id="volume-audit", node_id="fixture", mount_path_runtime=str(volume_root),
        online=True, writable=True, total_bytes=1000, free_bytes=500,
    )
    asset = LocalAsset(
        id="asset-missing", source_recording_id=source.id, asset_role="source_archive",
        storage_volume_id=volume.id, relative_path="recordings/missing.mp4", size_bytes=20,
        sha256="a" * 64, verification_state="verified", lifecycle_state="present",
    )
    job = ArchiveJob(
        id="job-stale-audit", source_recording_id=source.id, state="downloading",
        worker_id="worker-fixture", lease_token="lease-fixture",
        lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.flush()
    session.add(source)
    session.flush()
    session.add(volume)
    session.flush()
    session.add_all([asset, job])
    session.flush()

    report = audit_archive_state(
        session,
        now=datetime.now(timezone.utc),
        include_file_orphans=True,
    )

    codes = {finding["code"] for finding in report["findings"]}
    assert report["schema_name"] == "p16.archive.reconciliation"
    assert report["read_only"] is True
    assert report["automatic_repairs"] is False
    assert "DB_ASSET_FILE_MISSING" in codes
    assert "FILE_WITHOUT_DB_ASSET" in codes
    assert "PART_FILE_PRESENT" in codes
    assert "QUARANTINED_FILE_PRESENT" in codes
    assert "STALE_ARCHIVE_LEASE" in codes
    assert report["backlog"]["available_sources"] == 0  # the active job owns the source


def test_archive_audit_rejects_path_traversal_without_touching_outside_files(session, tmp_path) -> None:
    nvr = NvrDevice(id="nvr-audit-safe", name="NVR", host="fixture")
    camera = CameraTrack(id="camera-audit-safe", nvr_id=nvr.id, track_id="101", camera_name="Camera")
    source = SourceRecording(
        id="source-audit-safe", nvr_id=nvr.id, track_id=camera.track_id,
        source_start=datetime(2026, 10, 18, tzinfo=timezone.utc),
        source_end=datetime(2026, 10, 18, 0, 0, 10, tzinfo=timezone.utc),
        duration_ms=10_000, source_state="available",
    )
    volume = StorageVolume(
        id="volume-audit-safe", node_id="fixture", mount_path_runtime=str(tmp_path), online=True, writable=True,
    )
    asset = LocalAsset(
        id="asset-unsafe", source_recording_id=source.id, asset_role="source_archive",
        storage_volume_id=volume.id, relative_path="../outside.mp4", size_bytes=0,
        verification_state="verified", lifecycle_state="present",
    )
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.flush()
    session.add(source)
    session.flush()
    session.add(volume)
    session.flush()
    session.add(asset)
    session.flush()

    report = audit_archive_state(session)
    assert any(item["code"] == "ASSET_PATH_UNSAFE" for item in report["findings"])
    assert not (tmp_path.parent / "outside.mp4").exists()
