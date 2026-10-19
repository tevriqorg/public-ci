from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from analysis_worker.model_runner import (
    FailingFixtureModelRunner,
    FixtureEventTemplate,
    FixtureModelRunner,
)
from app.domain.analysis_contract import (
    AnalysisRequest,
    AnalysisResult,
    EventResult,
    EvidenceRef,
    ModelRunner,
    ModelRunnerError,
    RunnerIdentity,
)
from app.domain.analysis_request import build_analysis_request
from app.domain.analysis_result import AnalysisResultError, execute_runner, persist_result
from app.domain.manifest import create_analysis_run, create_profile_revision, upsert_source_recording
from app.models import (
    AnalysisArtifact,
    AnalysisEvent,
    CameraTrack,
    LocalAsset,
    NvrDevice,
    StorageVolume,
)


def seed_w6(session):
    nvr = NvrDevice(id="home-nvr-w6", name="Home NVR", host="fixture.local")
    track = CameraTrack(id="camera-w6", nvr_id=nvr.id, track_id="201", camera_name="Living Room")
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
        locator_fingerprint="fixture-w6",
    )
    volume = StorageVolume(
        id="archive-w6",
        node_id="fixture",
        mount_path_runtime="/fixture/w6",
        online=True,
        writable=True,
    )
    session.add(volume)
    session.flush()
    asset = LocalAsset(
        id="asset-w6",
        source_recording_id=source.id,
        storage_volume_id=volume.id,
        relative_path="private/source.mp4",
        verification_state="verified",
        lifecycle_state="present",
    )
    session.add(asset)
    session.flush()

    profile = create_profile_revision(
        session,
        profile_id="family-w6",
        revision=1,
        name="Family W6",
        payload={
            "semantic_intent": "describe relevant household activity",
            "prompt_schema": {"events": [{"type": "string", "summary": "string"}]},
        },
    )
    run = create_analysis_run(
        session,
        run_id="run-w6-1",
        asset_id=asset.id,
        profile=profile,
        pipeline_version="w6-fixture",
    )
    artifact = AnalysisArtifact(
        id="artifact-w6-1",
        analysis_run_id=run.id,
        asset_id=asset.id,
        artifact_type="candidate_clip",
        reference_location="/private/fixture/candidate.mp4",
        offset_or_range_json='{"start_offset_ms":1000,"end_offset_ms":5000}',
    )
    session.add(artifact)
    session.flush()
    return asset, profile, run, artifact


def test_fixture_runner_satisfies_model_runner_contract(session) -> None:
    _asset, _profile, run, artifact = seed_w6(session)
    request = build_analysis_request(session, run=run)
    runner = FixtureModelRunner(
        events=(
            FixtureEventTemplate(
                start_offset_ms=1200,
                end_offset_ms=3200,
                event_type="person_activity",
                summary="A person moved through the room.",
                confidence=0.9,
                evidence_artifact_ids=(artifact.id,),
            ),
        )
    )
    assert isinstance(runner, ModelRunner)
    result = runner.analyze(request)
    assert result.run_id == run.id
    assert result.asset_id == run.asset_id
    assert result.events[0].evidence_artifact_ids == (artifact.id,)


def test_valid_result_persists_event_provenance_and_is_idempotent(session) -> None:
    asset, _profile, run, artifact = seed_w6(session)
    request = build_analysis_request(session, run=run)
    runner = FixtureModelRunner(
        events=(
            FixtureEventTemplate(
                start_offset_ms=1200,
                end_offset_ms=3200,
                event_type="person_activity",
                summary="A person moved through the room.",
                confidence=0.9,
                evidence_artifact_ids=(artifact.id,),
            ),
        ),
        model_id="fixture-semantic",
        model_version="fixture-2",
        runtime_backend="fixture-runtime",
    )
    first = execute_runner(session, run=run, request=request, runner=runner)
    second = execute_runner(session, run=run, request=request, runner=runner)

    assert first[0].id == second[0].id
    count = session.scalar(select(func.count()).select_from(AnalysisEvent).where(AnalysisEvent.analysis_run_id == run.id))
    assert count == 1
    assert run.state == "completed"
    assert run.model_id == "fixture-semantic"
    assert run.model_version == "fixture-2"
    assert run.runtime_backend == "fixture-runtime"
    assert asset.verification_state == "verified"
    assert asset.lifecycle_state == "present"


def test_mismatched_result_identity_is_rejected(session) -> None:
    _asset, _profile, run, _artifact = seed_w6(session)
    request = build_analysis_request(session, run=run)
    bad = AnalysisResult(run_id="another-run", asset_id=run.asset_id, events=())
    with pytest.raises(AnalysisResultError, match="ANALYSIS_RESULT_IDENTITY_MISMATCH"):
        persist_result(
            session,
            run=run,
            request=request,
            identity=RunnerIdentity("fixture", "v1", "fixture"),
            result=bad,
        )


def test_invalid_offsets_and_confidence_are_rejected(session) -> None:
    _asset, _profile, run, _artifact = seed_w6(session)
    request = build_analysis_request(session, run=run)
    invalid_events = (
        EventResult(-1, 10, "motion", "bad start"),
        EventResult(20, 10, "motion", "bad end"),
        EventResult(0, 10, "motion", "bad confidence", confidence=1.5),
    )
    for event in invalid_events:
        with pytest.raises(AnalysisResultError):
            persist_result(
                session,
                run=run,
                request=request,
                identity=RunnerIdentity("fixture", "v1", "fixture"),
                result=AnalysisResult(run_id=run.id, asset_id=run.asset_id, events=(event,)),
            )


def test_foreign_artifact_reference_is_rejected(session) -> None:
    _asset, _profile, run, _artifact = seed_w6(session)

    other_source = upsert_source_recording(
        session,
        nvr_id="home-nvr-w6",
        track_id="201",
        source_start=datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc),
        source_end=datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc),
        duration_ms=3_600_000,
        locator_fingerprint="fixture-w6-other",
    )
    other_asset = LocalAsset(
        id="asset-w6-other",
        source_recording_id=other_source.id,
        storage_volume_id="archive-w6",
        relative_path="private/other.mp4",
        verification_state="verified",
        lifecycle_state="present",
    )
    session.add(other_asset)
    session.flush()
    other_profile = create_profile_revision(
        session,
        profile_id="other-w6",
        revision=1,
        name="Other W6",
        payload={"semantic_intent": "other"},
    )
    other_run = create_analysis_run(
        session,
        run_id="run-w6-other",
        asset_id=other_asset.id,
        profile=other_profile,
        pipeline_version="w6-fixture",
    )
    foreign = AnalysisArtifact(
        id="artifact-w6-foreign",
        analysis_run_id=other_run.id,
        asset_id=other_asset.id,
        artifact_type="keyframe",
        reference_location="/private/foreign.jpg",
    )
    session.add(foreign)
    session.flush()

    request = build_analysis_request(session, run=run)
    result = AnalysisResult(
        run_id=run.id,
        asset_id=run.asset_id,
        events=(
            EventResult(
                100,
                200,
                "motion",
                "foreign evidence",
                evidence_artifact_ids=(foreign.id,),
            ),
        ),
    )
    with pytest.raises(AnalysisResultError, match="ANALYSIS_EVIDENCE_FOREIGN"):
        persist_result(
            session,
            run=run,
            request=request,
            identity=RunnerIdentity("fixture", "v1", "fixture"),
            result=result,
        )


def test_runner_failure_marks_run_failed_without_mutating_asset(session) -> None:
    asset, _profile, run, _artifact = seed_w6(session)
    request = build_analysis_request(session, run=run)
    runner = FailingFixtureModelRunner("MODEL_TIMEOUT")
    before = (asset.verification_state, asset.lifecycle_state)

    with pytest.raises(ModelRunnerError, match="MODEL_TIMEOUT"):
        execute_runner(session, run=run, request=request, runner=runner)

    assert run.state == "failed"
    assert run.error == "MODEL_TIMEOUT"
    assert (asset.verification_state, asset.lifecycle_state) == before == ("verified", "present")


def test_reanalysis_keeps_independent_model_provenance(session) -> None:
    _asset, profile, run1, _artifact = seed_w6(session)
    request1 = build_analysis_request(session, run=run1)
    execute_runner(
        session,
        run=run1,
        request=request1,
        runner=FixtureModelRunner(model_id="candidate-a", model_version="1", runtime_backend="backend-a"),
    )

    run2 = create_analysis_run(
        session,
        run_id="run-w6-2",
        asset_id=run1.asset_id,
        profile=profile,
        pipeline_version="w6-fixture",
    )
    request2 = build_analysis_request(session, run=run2)
    execute_runner(
        session,
        run=run2,
        request=request2,
        runner=FixtureModelRunner(model_id="candidate-b", model_version="2", runtime_backend="backend-b"),
    )

    assert (run1.model_id, run1.model_version, run1.runtime_backend) == ("candidate-a", "1", "backend-a")
    assert (run2.model_id, run2.model_version, run2.runtime_backend) == ("candidate-b", "2", "backend-b")


def test_model_contract_has_no_infrastructure_or_external_policy_fields() -> None:
    forbidden = {"nvr", "credential", "password", "secret", "omv", "path", "aibox", "scene"}
    for contract in (RunnerIdentity, EvidenceRef, AnalysisRequest, EventResult, AnalysisResult):
        names = {field.name.lower() for field in fields(contract)}
        assert not any(any(token in name for token in forbidden) for name in names), (contract.__name__, names)

    request = AnalysisRequest(
        run_id="run",
        asset_id="asset",
        profile_id="profile",
        profile_revision=1,
        profile_snapshot_hash="a" * 64,
        semantic_intent="fixture",
        output_schema_json=None,
        media_ref="home-vision://asset/asset",
        evidence=(),
    )
    assert request.media_ref == "home-vision://asset/asset"
