from datetime import datetime, timezone
import hashlib
from pathlib import Path
from unittest.mock import patch
from datetime import timedelta

from app.domain.reconciliation import reconcile_storage_files
from app.models import CameraTrack, LocalAsset, NvrDevice, SourceRecording, StorageVolume


def test_s012_reports_storage_ledger_mismatches_without_mutating_rows_or_files(
    session, tmp_path: Path
):
    nvr = NvrDevice(id="s012-nvr", name="S012 synthetic", host="127.0.0.1")
    camera = CameraTrack(id="s012-camera", nvr_id=nvr.id, track_id="202",
                         camera_name="S012 synthetic")
    volume = StorageVolume(
        id="s012-volume", node_id="fixture", mount_path_runtime=str(tmp_path),
        total_bytes=1024**4, free_bytes=900 * 1024**3, online=True, writable=True,
    )
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.add(volume)
    session.flush()

    media_root = tmp_path / "recordings/2026/08/27/camera-202"
    deleted_path = media_root / "060001-source-deleted.mp4"
    clean_path = media_root / "060002-source-clean.mp4"
    file_only_path = media_root / "060003-untracked.mp4"
    deleted_path.parent.mkdir(parents=True)
    deleted_path.write_bytes(b"deleted-but-still-present")
    clean_path.write_bytes(b"clean-control-bytes")
    file_only_path.write_bytes(b"file-only-media-bytes")
    original_files = {
        path: path.read_bytes() for path in (deleted_path, clean_path, file_only_path)
    }

    starts = {
        "db-only": datetime(2026, 8, 27, 6, 0, 0, tzinfo=timezone.utc),
        "deleted": datetime(2026, 8, 27, 6, 0, 1, tzinfo=timezone.utc),
        "clean": datetime(2026, 8, 27, 6, 0, 2, tzinfo=timezone.utc),
    }
    rows = []
    for suffix, start in starts.items():
        source = SourceRecording(
            id=f"s012-source-{suffix}", nvr_id=nvr.id, track_id=camera.track_id,
            source_start=start, source_end=start.replace(second=start.second + 10),
            duration_ms=10_000, source_state="available",
        )
        session.add(source)
        session.flush()
        relative_path = {
            "db-only": "recordings/2026/08/27/camera-202/060000-source-db-only.mp4",
            "deleted": deleted_path.relative_to(tmp_path).as_posix(),
            "clean": clean_path.relative_to(tmp_path).as_posix(),
        }[suffix]
        asset = LocalAsset(
            id=f"s012-asset-{suffix}", source_recording_id=source.id,
            storage_volume_id=volume.id, relative_path=relative_path,
            size_bytes=17, sha256="a" * 64, verification_state="verified",
            lifecycle_state="deleted" if suffix == "deleted" else "present",
        )
        session.add(asset)
        rows.append(asset)
    session.flush()

    row_snapshot = [
        (row.id, row.lifecycle_state, row.verification_state, row.relative_path)
        for row in rows
    ]

    clean_asset = next(row for row in rows if row.id == "s012-asset-clean")
    independent_sha = hashlib.sha256(clean_path.read_bytes()).hexdigest()
    assert independent_sha != clean_asset.sha256
    # C2 canary: a bad-hash mutant that echoes the ledger value must be killed
    # by an oracle derived independently from the synthetic media bytes.
    with patch(
        "app.domain.reconciliation._sha256_file", return_value=clean_asset.sha256
    ):
        mutant_result = reconcile_storage_files(session)
    assert not any(
        finding.code == "ASSET_SHA256_MISMATCH" for finding in mutant_result.findings
    )
    assert independent_sha != clean_asset.sha256

    result = reconcile_storage_files(session)

    assert {
        (finding.code, finding.relative_path, finding.asset_id)
        for finding in result.findings
    } == {
        ("DB_ONLY_ASSET", "recordings/2026/08/27/camera-202/060000-source-db-only.mp4",
         "s012-asset-db-only"),
        ("DELETED_FILE_PRESENT", deleted_path.relative_to(tmp_path).as_posix(),
         "s012-asset-deleted"),
        ("FILE_ONLY_MEDIA", file_only_path.relative_to(tmp_path).as_posix(), None),
        ("ASSET_SHA256_MISMATCH", clean_path.relative_to(tmp_path).as_posix(),
         "s012-asset-clean"),
        ("ASSET_SIZE_MISMATCH", clean_path.relative_to(tmp_path).as_posix(),
         "s012-asset-clean"),
    }
    assert not result.clean
    assert [
        (row.id, row.lifecycle_state, row.verification_state, row.relative_path)
        for row in rows
    ] == row_snapshot
    assert {path: path.read_bytes() for path in original_files} == original_files
    assert not session.new and not session.dirty and not session.deleted

    repeated = reconcile_storage_files(session)
    assert repeated == result
    assert {path: path.read_bytes() for path in original_files} == original_files


def test_s012_reports_unreadable_recordings_subtree_instead_of_clean_result(
    session, tmp_path: Path, monkeypatch
):
    nvr = NvrDevice(id="s012-unreadable-nvr", name="S012 synthetic", host="127.0.0.1")
    session.add(nvr)
    session.add(StorageVolume(
        id="s012-unreadable-volume", node_id="fixture",
        mount_path_runtime=str(tmp_path), total_bytes=1024**4,
        free_bytes=900 * 1024**3, online=True, writable=True,
    ))
    session.flush()
    blocked = tmp_path / "recordings" / "unreadable-camera"
    blocked.mkdir(parents=True)
    (blocked / "untracked.mp4").write_bytes(b"synthetic hidden media")

    def walk_with_permission_fault(top, *, onerror, followlinks):
        assert Path(top) == tmp_path / "recordings"
        error = PermissionError(13, "synthetic permission fault", str(blocked))
        onerror(error)
        yield str(top), [blocked.name], []

    monkeypatch.setattr("app.domain.reconciliation.os.walk", walk_with_permission_fault)
    result = reconcile_storage_files(session)

    assert not result.clean
    assert {
        (finding.code, finding.relative_path)
        for finding in result.findings
    } == {("RECORDINGS_SUBTREE_UNREADABLE", "recordings/unreadable-camera")}


def test_component_reconciles_fifteen_days_of_historical_media_files(session, tmp_path: Path):
    """A historical file mutation is visible to the read-only ledger audit."""
    nvr = NvrDevice(id="s032-history-nvr", name="S032 synthetic", host="127.0.0.1")
    camera = CameraTrack(id="s032-history-camera", nvr_id=nvr.id, track_id="202",
                         camera_name="S032 synthetic")
    volume = StorageVolume(
        id="s032-history-volume", node_id="fixture", mount_path_runtime=str(tmp_path),
        total_bytes=1024**4, free_bytes=900 * 1024**3, online=True, writable=True,
    )
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.add(volume)
    session.flush()

    first_day = datetime.now(timezone.utc).date() - timedelta(days=14)
    snapshots = {}
    expected_hashes = {}
    for offset in range(15):
        day = first_day + timedelta(days=offset)
        payload = f"synthetic historical video {day.isoformat()}".encode()
        relative = Path("recordings") / day.isoformat() / "camera-202" / "segment.mp4"
        media_path = tmp_path / relative
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(payload)
        start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        source = SourceRecording(
            id=f"s032-history-source-{offset:02d}", nvr_id=nvr.id,
            track_id=camera.track_id, source_start=start,
            source_end=start + timedelta(seconds=30), duration_ms=30_000,
            source_state="available",
        )
        session.add(source)
        session.flush()
        session.add(LocalAsset(
            id=f"s032-history-asset-{offset:02d}", source_recording_id=source.id,
            storage_volume_id=volume.id, relative_path=relative.as_posix(),
            size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest(),
            verification_state="verified", lifecycle_state="present",
        ))
        snapshots[media_path] = payload
        expected_hashes[media_path] = hashlib.sha256(payload).hexdigest()
    session.flush()

    rows_before = [
        (asset.id, asset.relative_path, asset.size_bytes, asset.sha256,
         asset.lifecycle_state, asset.verification_state)
        for asset in session.query(LocalAsset).filter_by(storage_volume_id=volume.id).all()
    ]
    assert len(rows_before) == 15
    for path, payload in snapshots.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_hashes[path]

    # Mutate a past file without changing size; an independent bytes-based SHA
    # oracle must identify exactly the affected historical ledger row.
    mutated_path = sorted(snapshots)[3]
    original_bytes = mutated_path.read_bytes()
    mutated_bytes = bytes([original_bytes[0] ^ 1]) + original_bytes[1:]
    assert len(mutated_bytes) == len(original_bytes)
    mutated_path.write_bytes(mutated_bytes)
    independent_mutated_sha = hashlib.sha256(mutated_path.read_bytes()).hexdigest()
    assert independent_mutated_sha != expected_hashes[mutated_path]

    changed = reconcile_storage_files(session)
    mismatch = [finding for finding in changed.findings
                if finding.code == "ASSET_SHA256_MISMATCH"]
    assert [(finding.relative_path, finding.asset_id) for finding in mismatch] == [
        (mutated_path.relative_to(tmp_path).as_posix(), f"s032-history-asset-03")
    ]
    assert not any(finding.code == "ASSET_SIZE_MISMATCH" for finding in changed.findings)
    assert mutated_path.read_bytes() == mutated_bytes
    assert [
        (asset.id, asset.relative_path, asset.size_bytes, asset.sha256,
         asset.lifecycle_state, asset.verification_state)
        for asset in session.query(LocalAsset).filter_by(storage_volume_id=volume.id).all()
    ] == rows_before

    mutated_path.write_bytes(original_bytes)
    restored = reconcile_storage_files(session)
    assert restored.clean
    assert len([path for path in (tmp_path / "recordings").rglob("*.mp4")]) == 15
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == expected_hashes[path]
               for path in snapshots)
    assert [
        (asset.id, asset.relative_path, asset.size_bytes, asset.sha256,
         asset.lifecycle_state, asset.verification_state)
        for asset in session.query(LocalAsset).filter_by(storage_volume_id=volume.id).all()
    ] == rows_before
