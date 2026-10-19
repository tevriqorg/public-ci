from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.domain.manifest import (
    create_analysis_run,
    create_profile_revision,
    effective_archive_status,
    effective_archive_statuses,
    upsert_source_recording,
)
from app.models import ArchiveSourceSnapshot, CameraTrack, LocalAsset, NvrDevice, StorageVolume, Tombstone


def seed_source(session):
    nvr = NvrDevice(id="home-nvr-01", name="Home NVR", host="fixture.local")
    track = CameraTrack(id="camera-living", nvr_id=nvr.id, track_id="201", camera_name="Living Room")
    session.add(nvr)
    session.flush()
    session.add(track)
    session.flush()
    start = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
    src = upsert_source_recording(
        session,
        nvr_id=nvr.id,
        track_id=track.track_id,
        source_start=start,
        source_end=start + timedelta(minutes=72),
        duration_ms=72 * 60 * 1000,
        locator_fingerprint="fixture-fp-1",
    )
    return src


def test_source_identity_upsert_is_idempotent(session) -> None:
    source = seed_source(session)
    original_id = source.id
    source2 = upsert_source_recording(
        session,
        nvr_id=source.nvr_id,
        track_id=source.track_id,
        source_start=source.source_start,
        source_end=source.source_end,
        duration_ms=source.duration_ms,
        locator_fingerprint="fixture-fp-2",
    )
    assert source2.id == original_id
    assert source2.locator_fingerprint == "fixture-fp-2"


def test_source_identity_reuses_rolling_start_and_keeps_longest_end(session) -> None:
    source = seed_source(session)
    original_end = source.source_end
    later = upsert_source_recording(
        session,
        nvr_id=source.nvr_id,
        track_id=source.track_id,
        source_start=source.source_start,
        source_end=original_end + timedelta(minutes=10),
        duration_ms=source.duration_ms + 600000,
        locator_fingerprint="fixture-fp-longer",
    )
    shorter = upsert_source_recording(
        session,
        nvr_id=source.nvr_id,
        track_id=source.track_id,
        source_start=source.source_start,
        source_end=original_end - timedelta(minutes=10),
        duration_ms=source.duration_ms - 600000,
        locator_fingerprint="fixture-fp-shorter",
    )
    assert later.id == shorter.id == source.id
    assert shorter.source_end == original_end + timedelta(minutes=10)
    assert shorter.locator_fingerprint == "fixture-fp-longer"


def test_source_identity_unique_constraint(session) -> None:
    source = seed_source(session)
    from app.models import SourceRecording

    duplicate = SourceRecording(
        id="duplicate",
        nvr_id=source.nvr_id,
        track_id=source.track_id,
        source_start=source.source_start,
        source_end=source.source_end,
        duration_ms=source.duration_ms,
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_tombstone_overrides_verified_asset(session) -> None:
    source = seed_source(session)
    volume = StorageVolume(id="archive-a", node_id="fixture", mount_path_runtime="/fixture/a", online=True, writable=True)
    asset = LocalAsset(
        id="asset-1",
        source_recording_id=source.id,
        storage_volume_id=volume.id,
        relative_path="2026/08/27/a.mp4",
        verification_state="verified",
        lifecycle_state="present",
    )
    session.add(volume)
    session.flush()
    session.add(asset)
    session.flush()
    assert effective_archive_status(session, source.id) == "archived_legacy_unbound"
    session.add(Tombstone(id="tomb-1", source_recording_id=source.id, local_asset_id=asset.id, deleted_by="human", suppress_redownload=True))
    session.flush()
    assert effective_archive_status(session, source.id) == "deleted_by_user"


def test_archive_status_requires_current_source_snapshot_and_source_role(session) -> None:
    source = seed_source(session)
    volume = StorageVolume(id="archive-a", node_id="fixture", mount_path_runtime="/fixture/a", online=True, writable=True)
    snapshot = ArchiveSourceSnapshot(
        id="snapshot-1", source_recording_id=source.id, nvr_id=source.nvr_id,
        track_id=source.track_id, source_start=source.source_start,
        source_end=source.source_end, duration_ms=source.duration_ms,
        locator_fingerprint=source.locator_fingerprint,
    )
    session.add_all([volume, snapshot])
    session.flush()
    normalized = LocalAsset(
        id="normalized-1", source_recording_id=source.id, asset_role="normalized_playback",
        storage_volume_id=volume.id, relative_path="normalized.mp4", verification_state="verified", lifecycle_state="present",
    )
    source_asset = LocalAsset(
        id="source-1", source_recording_id=source.id, asset_role="source_archive",
        archive_source_snapshot_id=snapshot.id, storage_volume_id=volume.id,
        relative_path="source.mp4", verification_state="verified", lifecycle_state="present",
    )
    session.add_all([normalized, source_asset])
    session.flush()
    assert effective_archive_status(session, source.id) == "archived_verified"
    source.source_end += timedelta(minutes=10)
    assert effective_archive_status(session, source.id) == "archived_older_snapshot"


def test_bulk_archive_statuses_match_single_status_and_report_missing_source(session) -> None:
    source = seed_source(session)
    assert effective_archive_statuses(session, [source.id, "missing-source"]) == {
        source.id: "not_archived",
        "missing-source": "unknown",
    }


def test_analysis_profile_revision_snapshot_is_immutable_on_run(session) -> None:
    source = seed_source(session)
    volume = StorageVolume(id="archive-a", node_id="fixture", mount_path_runtime="/fixture/a", online=True, writable=True)
    asset = LocalAsset(id="asset-1", source_recording_id=source.id, storage_volume_id=volume.id, relative_path="a.mp4", verification_state="verified", lifecycle_state="present")
    session.add(volume)
    session.flush()
    session.add(asset)
    session.flush()
    p1 = create_profile_revision(session, profile_id="family-daily", revision=1, name="Family Daily", payload={"semantic_intent": "general household activity"})
    run = create_analysis_run(session, run_id="run-1", asset_id=asset.id, profile=p1, pipeline_version="v1")
    old_hash = run.analysis_profile_snapshot_hash
    p2 = create_profile_revision(session, profile_id="family-daily", revision=2, name="Family Daily", payload={"semantic_intent": "focus on doorway activity"})
    session.flush()
    assert p2.snapshot_hash != old_hash
    assert run.analysis_profile_revision == 1
    assert run.analysis_profile_snapshot_hash == old_hash
