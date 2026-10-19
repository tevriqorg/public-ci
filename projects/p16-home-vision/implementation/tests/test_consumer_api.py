import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_session
from app.domain.manifest import create_analysis_run, create_profile_revision, upsert_source_recording
from app.main import app
from app.models import (
    AnalysisArtifact,
    AnalysisEvent,
    ArchiveJob,
    CameraTrack,
    LocalAsset,
    NvrDevice,
    StorageVolume,
    ScanRun,
    SourceRecording,
)


@pytest.fixture
def consumer_fixture(session, tmp_path):
    nvr = NvrDevice(id="home-nvr-api", name="Home NVR", host="fixture.local")
    camera = CameraTrack(id="camera-living", nvr_id=nvr.id, track_id="201", camera_name="Living Room")
    session.add(nvr)
    session.flush()
    session.add(camera)
    session.flush()

    session.add(ScanRun(
        id="scan-api",
        nvr_id=nvr.id,
        track_id=camera.track_id,
        query_start=datetime(2026, 8, 27, 0, tzinfo=timezone.utc),
        query_end=datetime(2026, 8, 27, 23, 59, tzinfo=timezone.utc),
        state="success",
        result_count=1,
        completed_at=datetime(2026, 8, 28, 0, tzinfo=timezone.utc),
    ))
    session.flush()

    start = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
    source = upsert_source_recording(
        session,
        nvr_id=nvr.id,
        track_id=camera.track_id,
        source_start=start,
        source_end=start + timedelta(minutes=72),
        duration_ms=72 * 60 * 1000,
        locator_fingerprint="fixture-api",
    )

    volume_root = tmp_path / "volume-a"
    media_path = volume_root / "media" / "asset.bin"
    media_path.parent.mkdir(parents=True)
    media_bytes = b"home-vision-controlled-media-fixture"
    media_path.write_bytes(media_bytes)

    volume = StorageVolume(
        id="archive-api-a",
        node_id="fixture",
        mount_path_runtime=str(volume_root),
        total_bytes=10_000_000,
        free_bytes=9_000_000,
        online=True,
        writable=True,
    )
    session.add(volume)
    session.flush()

    asset = LocalAsset(
        id="asset-api",
        source_recording_id=source.id,
        asset_role="normalized_playback",
        storage_volume_id=volume.id,
        relative_path="media/asset.bin",
        size_bytes=len(media_bytes),
        sha256="a" * 64,
        container="bin",
        verification_state="verified",
        lifecycle_state="present",
    )
    card_asset = LocalAsset(
        id="asset-api-card",
        source_recording_id=source.id,
        asset_role="source_archive",
        storage_volume_id=volume.id,
        relative_path="media/asset.bin",
        size_bytes=len(media_bytes),
        sha256="a" * 64,
        container="bin",
        verification_state="verified",
        lifecycle_state="present",
    )
    session.add_all([asset, card_asset])
    session.flush()

    profile = create_profile_revision(
        session,
        profile_id="family-api",
        revision=1,
        name="Family API",
        payload={"semantic_intent": "fixture analysis"},
    )
    run = create_analysis_run(
        session,
        run_id="run-api",
        asset_id=asset.id,
        profile=profile,
        pipeline_version="w8-fixture",
    )
    run.state = "completed"

    private_reference = str(volume_root / "internal" / "keyframe-001.jpg")
    artifact = AnalysisArtifact(
        id="artifact-api",
        analysis_run_id=run.id,
        asset_id=asset.id,
        artifact_type="keyframe",
        reference_location=private_reference,
        offset_or_range_json=json.dumps({"offset_ms": 1500}),
        metadata_json=json.dumps({"internal_note": "not consumer contract"}),
    )
    event = AnalysisEvent(
        id="event-api",
        analysis_run_id=run.id,
        asset_id=asset.id,
        start_offset_ms=1000,
        end_offset_ms=2000,
        event_type="motion",
        summary="Fixture motion",
        confidence=0.9,
        evidence_refs_json=json.dumps([private_reference]),
    )
    session.add_all([artifact, event])
    session.flush()

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            yield client, volume, asset, media_bytes, private_reference
    finally:
        app.dependency_overrides.pop(get_session, None)


def test_consumer_query_resources_and_no_internal_path_leak(consumer_fixture) -> None:
    client, _volume, _asset, _media_bytes, private_reference = consumer_fixture

    capabilities = client.get("/api/v1/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["mode"] == "consumer_query_only"
    assert capabilities.json()["consumer_surface"] == "read_only"
    assert capabilities.json()["internal_control_surface"] == "local_runtime_only"
    assert capabilities.json()["inbound_analysis_policy"] is False

    cameras = client.get("/api/v1/cameras").json()
    assert cameras["items"][0]["camera_id"] == "camera-living"

    volumes = client.get("/api/v1/storage-volumes").json()
    assert volumes["items"][0]["storage_identity"] == "OMV 归档卷 archive-api-a · 物理设备未登记"
    assert volumes["items"][0]["physical_device_id"] is None

    scans = client.get("/api/v1/scan-runs").json()
    assert scans["count"] == 1
    assert scans["items"][0]["scan_run_id"] == "scan-api"
    assert scans["items"][0]["state"] == "success"

    assets = client.get("/api/v1/assets").json()
    assert assets["items"][0]["asset_id"] == "asset-api-card"
    assert "relative_path" not in assets["items"][0]
    assert "storage_volume_id" not in assets["items"][0]

    runs = client.get("/api/v1/analysis-runs").json()
    assert runs["items"][0]["analysis_run_id"] == "run-api"
    assert "error" not in runs["items"][0]

    profiles = client.get("/api/v1/analysis-profiles").json()
    assert profiles["items"][0]["analysis_profile_id"] == "family-api"
    assert "prompt_schema_json" not in profiles["items"][0]

    events = client.get("/api/v1/events").json()
    assert events["items"][0]["event_id"] == "event-api"
    assert events["items"][0]["artifact_ids"] == ["artifact-api"]

    artifacts = client.get("/api/v1/artifacts").json()
    assert artifacts["items"][0]["artifact_id"] == "artifact-api"
    assert "reference_location" not in artifacts["items"][0]
    assert "metadata" not in artifacts["items"][0]

    serialized = json.dumps(
        {"assets": assets, "runs": runs, "events": events, "artifacts": artifacts},
        ensure_ascii=False,
    )
    assert private_reference not in serialized
    assert "fixture.local" not in serialized


def test_recordings_list_collapses_rolling_duplicates(consumer_fixture, session) -> None:
    client, _volume, _asset, _media_bytes, _private_reference = consumer_fixture
    source = session.scalar(select(SourceRecording).order_by(SourceRecording.id))
    assert source is not None
    session.add(SourceRecording(
        id="source-rolling-short",
        nvr_id=source.nvr_id,
        track_id=source.track_id,
        source_start=source.source_start,
        source_end=source.source_end - timedelta(minutes=10),
        duration_ms=source.duration_ms - 600000,
    ))
    session.flush()
    response = client.get("/api/v1/recordings")
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["items"][0]["source_recording_id"] == source.id


def test_archive_job_payload_exposes_live_progress_contract(consumer_fixture, session) -> None:
    client, _volume, asset, _media_bytes, _private_reference = consumer_fixture
    source = session.get(SourceRecording, asset.source_recording_id)
    source.source_size_if_known = 10_000
    session.add(ArchiveJob(
        id="job-progress",
        source_recording_id=source.id,
        state="downloading",
        bytes_received=4_250,
    ))
    session.flush()

    job = client.get("/api/v1/archive-jobs").json()["items"][0]
    assert job["state"] == "downloading"
    assert job["phase"] == "downloading"
    assert job["bytes_received"] == 4_250
    assert job["source_size_bytes"] == 10_000
    assert job["progress_percent"] == 42.5
    assert job["throughput_bytes_per_second"] is None
    assert job["error_code"] is None

    session.get(ArchiveJob, "job-progress").bytes_received = 12_000
    session.flush()
    assert client.get("/api/v1/archive-jobs").json()["items"][0]["progress_percent"] == 99

    session.get(ArchiveJob, "job-progress").state = "completed"
    session.flush()
    assert client.get("/api/v1/archive-jobs").json()["items"][0]["progress_percent"] == 100


def test_archive_job_detail_and_pagination_cursor_are_stable(consumer_fixture, session) -> None:
    client, _volume, asset, _media_bytes, _private_reference = consumer_fixture
    source = session.get(SourceRecording, asset.source_recording_id)
    session.add_all([
        ArchiveJob(id="job-page-1", source_recording_id=source.id, state="queued"),
        ArchiveJob(id="job-page-2", source_recording_id=source.id, state="completed"),
    ])
    session.flush()
    first = client.get("/api/v1/archive-jobs?limit=1").json()
    assert first["next_after_id"] == first["items"][0]["archive_job_id"]
    second = client.get(f"/api/v1/archive-jobs?limit=1&after_id={first['next_after_id']}").json()
    assert second["items"]
    detail = client.get("/api/v1/archive-jobs/job-page-1")
    assert detail.status_code == 200
    assert detail.json()["archive_job_id"] == "job-page-1"
    assert client.get("/api/v1/archive-jobs/no-such-job").json()["detail"] == "archive_job_not_found"


def test_controlled_media_serves_content_without_path_metadata(consumer_fixture) -> None:
    client, _volume, _asset, media_bytes, private_reference = consumer_fixture
    access = client.get("/api/v1/media/asset-api/access")
    assert access.status_code == 200
    assert access.json() == {
        "asset_id": "asset-api",
        "status": "available",
        "content_path": "/api/v1/media/asset-api/content",
        "playback_path": "/api/v1/media/asset-api/playback",
        "playback_mode": "original",
        "video_codec": None,
        "audio_codec": None,
        "container": "bin",
    }
    assert private_reference not in access.text

    content = client.get("/api/v1/media/asset-api/content")
    assert content.status_code == 200
    assert content.content == media_bytes
    assert private_reference not in str(content.headers)


def test_browser_playback_endpoint_preserves_archive_and_exposes_seekable_route(consumer_fixture) -> None:
    client, _volume, _asset, media_bytes, _private_reference = consumer_fixture
    response = client.get("/api/v1/media/asset-api/playback")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/octet-stream")
    assert response.headers["content-disposition"] == "inline"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.content == media_bytes
    ranged = client.get("/api/v1/media/asset-api/playback", headers={"Range": "bytes=0-3"})
    assert ranged.status_code == 206
    assert ranged.headers["content-range"] == f"bytes 0-3/{len(media_bytes)}"
    assert ranged.content == media_bytes[:4]
    assert client.get("/api/v1/media/asset-api/content").content == media_bytes


def test_thumbnail_endpoint_serves_persisted_sidecar_and_never_generates(consumer_fixture) -> None:
    client, volume, _asset, _media_bytes, _private_reference = consumer_fixture
    sidecar_path = Path(volume.mount_path_runtime) / "recordings" / "thumbnails" / "asset-api.jpg"
    sidecar_path.parent.mkdir(parents=True)
    sidecar_path.write_bytes(b"persisted-jpeg-fixture")
    (Path(volume.mount_path_runtime) / _asset.relative_path).unlink()
    response = client.get("/api/v1/media/asset-api/thumbnail")
    assert response.status_code == 200
    assert response.content == b"persisted-jpeg-fixture"
    assert response.headers["cache-control"] == "private, max-age=86400"
    sidecar_path.unlink()
    missing = client.get("/api/v1/media/asset-api/thumbnail")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "thumbnail_not_generated"


def test_media_access_distinguishes_unavailable_deleted_and_not_found(consumer_fixture, session) -> None:
    client, volume, asset, _media_bytes, _private_reference = consumer_fixture

    volume.online = False
    session.flush()
    unavailable = client.get("/api/v1/media/asset-api/access")
    assert unavailable.json()["status"] == "temporarily_unavailable"
    assert client.get("/api/v1/media/asset-api/content").status_code == 503

    volume.online = True
    asset.lifecycle_state = "deleted"
    session.flush()
    deleted = client.get("/api/v1/media/asset-api/access")
    assert deleted.json()["status"] == "deleted"
    assert client.get("/api/v1/media/asset-api/content").status_code == 410

    missing = client.get("/api/v1/media/no-such-asset/access")
    assert missing.status_code == 200
    assert missing.json()["status"] == "not_found"


def test_consumer_api_is_read_only_and_has_no_scene_policy_ingress(consumer_fixture) -> None:
    client, *_ = consumer_fixture
    openapi = client.get("/openapi.json").json()
    consumer_paths = {path: methods for path, methods in openapi["paths"].items() if path.startswith("/api/v1")}
    assert consumer_paths
    for path, methods in consumer_paths.items():
        assert set(methods) <= {"get", "parameters"}, path
        assert "scene" not in path.lower()
        assert "policy" not in path.lower()


def test_r2_projection_and_playback_use_only_completed_promoted_media(consumer_fixture, monkeypatch, tmp_path):
    client, _volume, asset, _media_bytes, _private_reference = consumer_fixture
    root = tmp_path / "r2"
    monkeypatch.setenv("HOME_VISION_R2_ASSET_ROOT", str(root))
    directory = root / asset.id
    (directory / "web").mkdir(parents=True)
    status = {"asset_id": asset.id, "state": "running", "raw_path": "/private/raw", "stages": {"web": "running"}}
    status_file = directory / "status.json"
    status_file.write_text(json.dumps(status))
    web = directory / "web" / "video.mp4"
    web.write_bytes(b"promoted-web-fixture")
    assert client.get(f"/api/r2/assets/{asset.id}/web").status_code == 404
    assert client.get(f"/api/v1/media/{asset.id}/access").json().get("playback_mode") != "r2_web_media"

    status.update(state="derived", stages={"web": "done", "frames": "done"})
    status_file.write_text(json.dumps(status))
    payload = client.get(f"/api/v1/assets/{asset.id}").json()
    assert payload["stages"] == status["stages"]
    assert "raw_path" not in payload
    access = client.get(f"/api/v1/media/{asset.id}/access").json()
    assert access["playback_mode"] == "r2_web_media"
    response = client.get(access["playback_path"], headers={"Range": "bytes=0-3"})
    assert response.status_code == 206
    assert response.content == b"prom"
    web.unlink()
    assert client.get(f"/api/v1/media/{asset.id}/access").json().get("playback_mode") != "r2_web_media"

    web.write_bytes(b"promoted-web-fixture")
    asset.lifecycle_state = "deleted"
    assert client.get(f"/api/v1/media/{asset.id}/access").json()["status"] == "deleted"
