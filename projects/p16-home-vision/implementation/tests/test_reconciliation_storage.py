from datetime import datetime, timezone
import hashlib
from pathlib import Path
from unittest.mock import patch

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
