from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app.domain.analysis_profile as analysis_profile_module
import app.domain.screening as screening_module
from app.domain.analysis_profile import (
    AnalysisProfileError,
    profile_snapshot_hash_from_input,
    select_current_profiles,
)
from app.domain.manifest import create_analysis_run, create_profile_revision, upsert_source_recording
from app.domain.screening import (
    FrameSample,
    ScreeningError,
    ScreeningPolicy,
    screen_samples,
    screening_policy_from_profile,
)
from app.models import CameraTrack, LocalAsset, NvrDevice, StorageVolume


def seed_verified_asset(session) -> LocalAsset:
    nvr = NvrDevice(id="home-nvr-w5", name="Home NVR", host="fixture.local")
    track = CameraTrack(id="camera-living", nvr_id=nvr.id, track_id="201", camera_name="Living Room")
    session.add(nvr)
    session.flush()
    session.add(track)
    session.flush()
    start = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
    source = upsert_source_recording(
        session,
        nvr_id=nvr.id,
        track_id=track.track_id,
        source_start=start,
        source_end=start + timedelta(minutes=72),
        duration_ms=72 * 60 * 1000,
        locator_fingerprint="fixture-w5",
    )
    volume = StorageVolume(
        id="archive-w5",
        node_id="fixture",
        mount_path_runtime="/fixture/w5",
        online=True,
        writable=True,
    )
    session.add(volume)
    session.flush()
    asset = LocalAsset(
        id="asset-w5",
        source_recording_id=source.id,
        storage_volume_id=volume.id,
        relative_path="fixture/w5.mp4",
        verification_state="verified",
        lifecycle_state="present",
    )
    session.add(asset)
    session.flush()
    return asset


def test_profile_hash_is_canonical_and_material_change_changes_it() -> None:
    first = profile_snapshot_hash_from_input(
        name="Family Daily",
        payload={
            "camera_scope": {"camera_ids": ["camera-living"], "mode": "include"},
            "screening_policy": {"merge_gap_ms": 2000, "frame_threshold": 0.08},
        },
    )
    reordered = profile_snapshot_hash_from_input(
        name="Family Daily",
        payload={
            "screening_policy": {"frame_threshold": 0.08, "merge_gap_ms": 2000},
            "camera_scope": {"mode": "include", "camera_ids": ["camera-living"]},
        },
    )
    changed = profile_snapshot_hash_from_input(
        name="Family Daily",
        payload={
            "screening_policy": {"frame_threshold": 0.20, "merge_gap_ms": 2000},
            "camera_scope": {"mode": "include", "camera_ids": ["camera-living"]},
        },
    )
    assert first == reordered
    assert changed != first


def test_profile_selection_uses_scope_and_latest_revision(session) -> None:
    common = {
        "camera_scope": {"camera_ids": ["camera-living"]},
        "time_scope": {
            "timezone": "Asia/Shanghai",
            "days": [6],
            "start": "18:00",
            "end": "23:00",
        },
    }
    p1 = create_profile_revision(session, profile_id="family", revision=1, name="Family", payload=common)
    p2 = create_profile_revision(session, profile_id="family", revision=2, name="Family", payload=common)
    other = create_profile_revision(
        session,
        profile_id="other-camera",
        revision=1,
        name="Other",
        payload={"camera_scope": {"camera_ids": ["camera-bedroom"]}},
    )
    selected = select_current_profiles(
        [p1, other, p2],
        camera_id="camera-living",
        at=datetime(2026, 8, 30, 10, 40, tzinfo=timezone.utc),
    )
    assert [(profile.id, profile.revision) for profile in selected] == [("family", 2)]


def test_run_provenance_is_pinned_and_mutated_profile_is_rejected(session) -> None:
    asset = seed_verified_asset(session)
    p1 = create_profile_revision(
        session,
        profile_id="family-daily",
        revision=1,
        name="Family Daily",
        payload={"semantic_intent": "general household activity"},
    )
    run = create_analysis_run(session, run_id="run-w5-1", asset_id=asset.id, profile=p1, pipeline_version="w5")
    pinned_hash = run.analysis_profile_snapshot_hash

    p2 = create_profile_revision(
        session,
        profile_id="family-daily",
        revision=2,
        name="Family Daily",
        payload={"semantic_intent": "doorway activity"},
    )
    assert p2.snapshot_hash != pinned_hash
    assert run.analysis_profile_revision == 1
    assert run.analysis_profile_snapshot_hash == pinned_hash

    p1.semantic_intent = "mutated without a new revision"
    with pytest.raises(AnalysisProfileError, match="ANALYSIS_PROFILE_SNAPSHOT_MISMATCH"):
        create_analysis_run(session, run_id="run-w5-invalid", asset_id=asset.id, profile=p1, pipeline_version="w5")


def test_screening_policy_loads_from_local_profile(session) -> None:
    profile = create_profile_revision(
        session,
        profile_id="screen",
        revision=1,
        name="Screen",
        payload={
            "screening_policy": {
                "frame_threshold": 0.5,
                "min_active_ratio": 0.5,
                "sample_stride": 2,
                "merge_gap_ms": 1500,
            }
        },
    )
    policy = screening_policy_from_profile(profile)
    assert policy == ScreeningPolicy(frame_threshold=0.5, min_active_ratio=0.5, sample_stride=2, merge_gap_ms=1500)


def test_static_frames_have_no_activity() -> None:
    samples = [FrameSample(index * 1000, bytes([10] * 16)) for index in range(5)]
    result = screen_samples(samples)
    assert result.active_frames == 0
    assert result.activity_ratio == 0
    assert not result.meaningful_activity
    assert result.candidates == ()


def test_changed_frames_produce_activity_and_bounded_candidate_merge() -> None:
    samples = [
        FrameSample(0, bytes([0] * 16)),
        FrameSample(1000, bytes([255] * 16)),
        FrameSample(2000, bytes([0] * 16)),
        FrameSample(6000, bytes([255] * 16)),
    ]
    result = screen_samples(
        samples,
        policy=ScreeningPolicy(frame_threshold=0.5, min_active_ratio=0.5, merge_gap_ms=1500),
    )
    assert result.active_frames == 3
    assert result.meaningful_activity
    assert len(result.candidates) == 2
    assert (result.candidates[0].start_ms, result.candidates[0].end_ms) == (1000, 2000)
    assert (result.candidates[1].start_ms, result.candidates[1].end_ms) == (6000, 6000)


def test_screening_failure_does_not_mutate_archive_truth(session) -> None:
    asset = seed_verified_asset(session)
    before = (asset.verification_state, asset.lifecycle_state)
    with pytest.raises(ScreeningError, match="SCREEN_FRAME_SHAPE_MISMATCH"):
        screen_samples([FrameSample(0, b"\x00"), FrameSample(1000, b"\x00\x01")])
    assert (asset.verification_state, asset.lifecycle_state) == before == ("verified", "present")


def test_w5_domain_has_no_aibox_dependency() -> None:
    for module in (analysis_profile_module, screening_module):
        source = Path(module.__file__).read_text(encoding="utf-8").lower()
        assert "aibox" not in source
