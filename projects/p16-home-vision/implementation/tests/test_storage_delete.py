from pathlib import Path

import pytest

from app.domain.archive_pipeline import sha256_file
from app.domain.deletion import (
    DeleteError,
    confirm_delete_intent,
    create_delete_preview,
    execute_confirmed_delete,
)
from app.domain.storage import StorageError, filesystem_smoke_check, select_storage_volume
from app.models import AuditEvent, CameraTrack, LocalAsset, NvrDevice, SourceRecording, StorageVolume, Tombstone

GIB = 1024**3


def make_volume(volume_id: str, *, free: int, online=True, writable=True, total=4 * 1024 * GIB) -> StorageVolume:
    return StorageVolume(
        id=volume_id,
        node_id="fixture",
        mount_path_runtime=f"/fixture/{volume_id}",
        total_bytes=total,
        free_bytes=free,
        online=online,
        writable=writable,
    )


def seed_asset(session, tmp_path: Path, *, suffix: str = "1"):
    nvr = NvrDevice(id=f"nvr-{suffix}", name="NVR", host="fixture")
    session.add(nvr)
    session.flush()
    track = CameraTrack(id=f"cam-{suffix}", nvr_id=nvr.id, track_id="201", camera_name="Cam")
    session.add(track)
    session.flush()
    from datetime import datetime

    source = SourceRecording(
        id=f"src-{suffix}",
        nvr_id=nvr.id,
        track_id="201",
        source_start=datetime(2026, 8, 27, 6),
        source_end=datetime(2026, 8, 27, 6, 0, 10),
        duration_ms=10000,
    )
    volume = make_volume(f"archive-{suffix}", free=500 * GIB)
    session.add_all([source, volume])
    session.flush()
    relative = f"archive/video-{suffix}.mp4"
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture-video" * 1024)
    asset = LocalAsset(
        id=f"asset-{suffix}",
        source_recording_id=source.id,
        storage_volume_id=volume.id,
        relative_path=relative,
        size_bytes=path.stat().st_size,
        sha256=sha256_file(path),
        verification_state="verified",
        lifecycle_state="present",
    )
    session.add(asset)
    session.flush()
    return asset, path


def test_allocator_ignores_unusable_and_uses_most_free() -> None:
    volumes = [
        make_volume("offline", free=900 * GIB, online=False),
        make_volume("readonly", free=800 * GIB, writable=False),
        make_volume("low", free=105 * GIB),
        make_volume("a", free=300 * GIB),
        make_volume("b", free=400 * GIB),
    ]
    selected = select_storage_volume(volumes, required_bytes=20 * GIB)
    assert selected.id == "b"
    with pytest.raises(StorageError, match="STORAGE_NOT_READY"):
        select_storage_volume([volumes[0], volumes[1], volumes[2]], required_bytes=20 * GIB)


def test_filesystem_smoke_check_leaves_no_artifact(tmp_path) -> None:
    smoke = tmp_path / "smoke"
    filesystem_smoke_check(smoke)
    assert list(smoke.iterdir()) == []


def test_unconfirmed_delete_cannot_unlink(session, tmp_path) -> None:
    asset, path = seed_asset(session, tmp_path)
    intent = create_delete_preview(session, intent_id="del-1", asset=asset, requested_by="human")
    with pytest.raises(DeleteError, match="DELETE_NOT_CONFIRMED"):
        execute_confirmed_delete(session, intent=intent, asset=asset, volume_root=tmp_path)
    assert path.exists()


def test_stale_preview_fails_closed(session, tmp_path) -> None:
    asset, path = seed_asset(session, tmp_path, suffix="2")
    intent = create_delete_preview(session, intent_id="del-2", asset=asset, requested_by="human")
    confirm_delete_intent(session, intent)
    path.write_bytes(b"changed")
    with pytest.raises(DeleteError, match="DELETE_PREVIEW_STALE_SIZE"):
        execute_confirmed_delete(session, intent=intent, asset=asset, volume_root=tmp_path)
    assert path.exists()
    assert intent.state == "failed"


def test_confirmed_delete_creates_tombstone_and_audit(session, tmp_path) -> None:
    asset, path = seed_asset(session, tmp_path, suffix="3")
    intent = create_delete_preview(session, intent_id="del-3", asset=asset, requested_by="human", reason="fixture")
    confirm_delete_intent(session, intent)
    tomb = execute_confirmed_delete(session, intent=intent, asset=asset, volume_root=tmp_path)
    assert not path.exists()
    assert intent.state == "completed"
    assert asset.lifecycle_state == "deleted"
    assert tomb.suppress_redownload is True
    assert session.get(Tombstone, "tomb-del-3") is not None
    assert session.get(AuditEvent, "audit-delete-del-3") is not None


def test_recovery_completes_after_unlink_before_db_commit(session, tmp_path) -> None:
    asset, path = seed_asset(session, tmp_path, suffix="4")
    intent = create_delete_preview(session, intent_id="del-4", asset=asset, requested_by="human")
    confirm_delete_intent(session, intent)
    intent.state = "deleting"
    session.flush()
    path.unlink()
    tomb = execute_confirmed_delete(session, intent=intent, asset=asset, volume_root=tmp_path)
    assert tomb.id == "tomb-del-4"
    assert intent.state == "completed"
    assert asset.lifecycle_state == "deleted"
