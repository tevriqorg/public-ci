from pathlib import Path

from transfer_hub.config import Settings
from transfer_hub.db import Store
from transfer_hub.domain import JobState
from transfer_hub.service import TransferService


def make_service(tmp_path: Path) -> tuple[TransferService, Store]:
    settings = Settings(
        data_dir=tmp_path,
        bind="127.0.0.1",
        port=0,
        api_token_file=tmp_path / "api.token",
        secrets_dir=tmp_path / "secrets",
        qqxfer_url="http://127.0.0.1:9",
        qqxfer_token_file=tmp_path / "missing-qqxfer-token",
    )
    settings.ensure_dirs()
    store = Store(tmp_path / "transfer.db")
    return TransferService(settings, store), store


def create_job(store: Store, job_id: str) -> dict:
    return store.create_job({
        "id": job_id,
        "source_type": "url",
        "source": "https://example.com/file.bin",
        "filename": "file.bin",
        "title": "file.bin",
        "engine": "aria2",
        "options": {
            "delivery_backend": "none",
            "ack_policy": "required",
        },
        "secret": {},
    })


def test_restart_marks_interrupted_jobs_failed(tmp_path: Path):
    service, store = make_service(tmp_path)
    create_job(store, "fetching")
    create_job(store, "paused")
    store.update("fetching", state=JobState.FETCHING.value, progress=42, download_speed=123)
    store.update("paused", state=JobState.PAUSED.value, progress=50)

    service._recover_after_restart()

    fetching = store.get_job("fetching")
    paused = store.get_job("paused")
    assert fetching["state"] == JobState.FAILED
    assert paused["state"] == JobState.FAILED
    assert "legacy active state FETCHING" in fetching["error"]
    assert "legacy active state PAUSED" in paused["error"]
    assert fetching["download_speed"] == 0

    events = store.recent_events("paused", 10)
    assert any(e["type"] == "job.recovered_after_restart" for e in events)


def test_restart_finishes_downloaded_cleanup(tmp_path: Path):
    service, store = make_service(tmp_path)
    create_job(store, "downloaded")
    store.update("downloaded", state=JobState.DOWNLOADED.value)

    work = tmp_path / "jobs" / "downloaded"
    work.mkdir(parents=True)
    (work / "artifact.bin").write_bytes(b"x")

    service._recover_after_restart()

    job = store.get_job("downloaded")
    assert job["state"] == JobState.DONE
    assert not work.exists()


def test_retry_cleans_workdir_and_resets_results(tmp_path: Path):
    service, store = make_service(tmp_path)
    create_job(store, "retry")
    store.update(
        "retry",
        state=JobState.FAILED.value,
        error="boom",
        progress=72,
        upload_progress=50,
        download_speed=100,
        upload_speed=200,
        eta_seconds=9,
        original_size=123,
        final_size=456,
        sha256="abc",
        artifact_path="/tmp/x",
        result={"backend": "none"},
        completed_at="2026-01-01T00:00:00+00:00",
    )
    work = tmp_path / "jobs" / "retry"
    work.mkdir(parents=True)
    (work / "partial.7z").write_bytes(b"partial")

    job = service.retry("retry")

    assert job["state"] == JobState.QUEUED
    assert job["error"] is None
    assert job["progress"] == 0
    assert job["upload_progress"] == 0
    assert job["download_speed"] == 0
    assert job["upload_speed"] == 0
    assert job["eta_seconds"] is None
    assert job["original_size"] is None
    assert job["final_size"] is None
    assert job["sha256"] is None
    assert job["artifact_path"] is None
    assert job["result"] is None
    assert job["completed_at"] is None
    assert not work.exists()


def test_stage_lease_recovery_reclaims_without_failing_job(tmp_path: Path):
    service, store = make_service(tmp_path)
    job = service.create_url_job({
        "source": "https://example.com/file.bin",
        "source_type": "url",
        "delivery_backend": "none",
    })
    claim = store.claim_stage_for_job(job["id"], "FETCH", "worker-a", lease_seconds=60)
    assert claim is not None
    store.update(job["id"], state=JobState.FETCHING.value)
    with store._connect() as conn:
        conn.execute("UPDATE job_stages SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (claim["id"],))

    service._recover_after_restart()

    recovered = store.get_job(job["id"])
    assert recovered["state"] == JobState.FETCHING
    event = [e for e in store.recent_events(job["id"], 20) if e["type"] == "job.recovered_after_restart"][-1]
    assert event["data"]["action"] == "stage_lease_recovery"
    reclaimed = Store(tmp_path / "transfer.db").claim_stage("FETCH", "worker-b", lease_seconds=60)
    assert reclaimed is not None
    assert reclaimed["id"] == claim["id"]
    assert reclaimed["claim_token"] != claim["claim_token"]


def test_failed_stage_retry_preserves_upstream_artifact(tmp_path: Path):
    service, store = make_service(tmp_path)
    job = service.create_url_job({
        "source": "https://example.com/file.bin",
        "source_type": "url",
        "delivery_backend": "none",
    })
    fetch = store.claim_stage_for_job(job["id"], "FETCH", "fetcher")
    assert fetch is not None
    assert store.finish_stage(fetch["id"], fetch["claim_token"], success=True)
    artifact_path = tmp_path / "jobs" / job["id"] / "source.bin"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(b"already-downloaded")
    artifact = store.add_artifact(job["id"], "FETCH", "source", str(artifact_path), size=18)
    store.ensure_stage(job["id"], "PROCESS", sequence=1)
    process = store.claim_stage_for_job(job["id"], "PROCESS", "processor")
    assert process is not None
    assert store.finish_stage(process["id"], process["claim_token"], success=False, error="packer failed")
    store.update(job["id"], state=JobState.FAILED.value, error="packer failed", completed_at="2026-01-01T00:00:00+00:00")

    retried = service.retry(job["id"])

    assert retried["id"] == job["id"]
    assert retried["state"] == JobState.VERIFYING
    assert retried["retries"] == 1
    assert artifact_path.exists()
    assert store.list_artifacts(job["id"], "FETCH")[0]["id"] == artifact["id"]
    process_stage = store.latest_stage(job["id"], "PROCESS")
    assert process_stage["state"] == "READY"


def test_failed_job_ttl_cleanup_removes_artifacts_but_preserves_failure(tmp_path: Path):
    service, store = make_service(tmp_path)
    job = create_job(store, "failed-expired")
    work = tmp_path / "jobs" / job["id"]
    work.mkdir(parents=True)
    (work / "partial.bin").write_bytes(b"partial")
    store.update(
        job["id"],
        state=JobState.FAILED.value,
        error="download failed",
        cleanup_at="2000-01-01T00:00:00+00:00",
        completed_at="2000-01-01T00:00:00+00:00",
    )

    service._cleanup_expired_failed()

    cleaned = store.get_job(job["id"])
    assert cleaned["state"] == JobState.FAILED
    assert cleaned["error"] == "download failed"
    assert cleaned["cleanup_at"] is None
    assert not work.exists()
    events = store.recent_events(job["id"], 10)
    assert any(e["type"] == "job.failed_ttl_cleanup" for e in events)


def test_failed_vps_local_job_ttl_cleanup_keeps_source_file(tmp_path: Path):
    service, store = make_service(tmp_path)
    source = tmp_path / "local-files" / "keep.apk"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"apk")
    job = service.create_local_job(source, "keep.apk", {"local_origin": "vps", "ttl_seconds": 1})
    work = tmp_path / "jobs" / job["id"]
    work.mkdir(parents=True, exist_ok=True)
    (work / "delivery.zip").write_bytes(b"zip")
    store.update(
        job["id"],
        state=JobState.FAILED.value,
        cleanup_at="2000-01-01T00:00:00+00:00",
        completed_at="2000-01-01T00:00:00+00:00",
    )

    service._cleanup_expired_failed()

    assert source.exists()
    assert not work.exists()
    assert store.get_job(job["id"])["state"] == JobState.FAILED
