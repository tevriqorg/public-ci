from datetime import datetime, timezone

import pytest

from app.domain.source_canonicalization import canonicalize_sources, CanonicalizationConflict
from app.models import ArchiveSourceSnapshot, CameraTrack, LocalAsset, NvrDevice, SourceRecording, StorageVolume


def _source(session, source_id: str, start: datetime, end: datetime) -> SourceRecording:
    nvr = session.get(NvrDevice, "nvr-canon")
    if nvr is None:
        nvr = NvrDevice(id="nvr-canon", name="NVR", host="fixture")
        session.add(nvr); session.flush()
        session.add(CameraTrack(id="camera-canon", nvr_id=nvr.id, track_id="101", camera_name="Camera")); session.flush()
    source = SourceRecording(id=source_id, nvr_id=nvr.id, track_id="101", source_start=start, source_end=end, duration_ms=int((end-start).total_seconds()*1000), source_state="available")
    session.add(source); session.flush()
    return source


def test_september_dry_run_proposes_longest_and_never_reads_august_as_mutation(session) -> None:
    start = datetime(2026, 9, 3, 17, 53, tzinfo=timezone.utc)
    short = _source(session, "source-short", start, start.replace(minute=54))
    long = _source(session, "source-long", start, start.replace(minute=55))
    _source(session, "source-august", datetime(2026, 8, 3, tzinfo=timezone.utc), datetime(2026, 8, 3, 0, 1, tzinfo=timezone.utc))
    session.commit()
    result = canonicalize_sources(session, month="2026-09", apply=False)
    assert result.groups[0].canonical_source_id == long.id
    assert result.groups[0].reparent_counts == {}
    assert session.get(SourceRecording, short.id) is not None
    assert session.get(SourceRecording, "source-august") is not None


def test_multiple_verified_archives_fail_closed(session, tmp_path) -> None:
    start = datetime(2026, 9, 3, 17, 53, tzinfo=timezone.utc)
    first = _source(session, "source-first", start, start.replace(minute=54))
    second = _source(session, "source-second", start, start.replace(minute=55))
    volume = StorageVolume(id="archive-a", node_id="fixture", mount_path_runtime=str(tmp_path), online=True, writable=True)
    session.add(volume); session.flush()
    for source, suffix in ((first, "first"), (second, "second")):
        snapshot = ArchiveSourceSnapshot(id=f"snapshot-{suffix}", source_recording_id=source.id, nvr_id=source.nvr_id, track_id=source.track_id, source_start=source.source_start, source_end=source.source_end, duration_ms=source.duration_ms)
        session.add(snapshot); session.flush()
        session.add(LocalAsset(id=f"asset-{suffix}", source_recording_id=source.id, archive_source_snapshot_id=snapshot.id, storage_volume_id=volume.id, relative_path=f"{suffix}.mp4", verification_state="verified", lifecycle_state="present"))
    session.flush()
    with pytest.raises(CanonicalizationConflict, match="multiple archive anchors"):
        canonicalize_sources(session, month="2026-09", apply=True)
    result = canonicalize_sources(session, month="2026-09", apply=True, allow_conflicts=True)
    assert result.applied is True
    assert session.get(SourceRecording, first.id) is not None
    assert session.get(SourceRecording, second.id) is not None


def test_identical_verified_archives_choose_longest_snapshot(session, tmp_path) -> None:
    start = datetime(2026, 9, 3, 17, 53, tzinfo=timezone.utc)
    first = _source(session, "source-identical-first", start, start.replace(minute=54))
    second = _source(session, "source-identical-second", start, start.replace(minute=55))
    volume = StorageVolume(id="archive-identical", node_id="fixture", mount_path_runtime=str(tmp_path), online=True, writable=True)
    session.add(volume); session.flush()
    proof = "a" * 64
    for source, suffix in ((first, "first"), (second, "second")):
        snapshot = ArchiveSourceSnapshot(id=f"snapshot-identical-{suffix}", source_recording_id=source.id, nvr_id=source.nvr_id, track_id=source.track_id, source_start=source.source_start, source_end=source.source_end, duration_ms=source.duration_ms)
        session.add(snapshot); session.flush()
        session.add(LocalAsset(id=f"asset-identical-{suffix}", source_recording_id=source.id, archive_source_snapshot_id=snapshot.id, storage_volume_id=volume.id, relative_path=f"{suffix}.mp4", size_bytes=100, sha256=proof, verification_state="verified", lifecycle_state="present"))
    session.flush()
    result = canonicalize_sources(session, month="2026-09", apply=False)
    assert result.groups[0].canonical_source_id == second.id
    result = canonicalize_sources(session, month="2026-09", apply=True)
    assert result.applied is True
    assert session.get(SourceRecording, first.id) is None
    assert session.get(SourceRecording, second.id) is not None
