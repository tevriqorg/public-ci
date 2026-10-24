from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from transfer_hub.db import Store
from transfer_hub.domain import JobState


def test_store_job_and_events(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    job = store.create_job({
        "id": "j1",
        "source_type": "url",
        "source": "https://example.com/a.bin",
        "filename": "a.bin",
        "title": "a",
        "engine": "aria2",
        "options": {"delivery_backend": "none"},
        "secret": {},
    })
    assert job["state"] == "QUEUED"
    job = store.transition("j1", JobState.FETCHING)
    assert job["state"] == "FETCHING"
    assert len(store.events_after(0, "j1")) >= 2


def test_store_migrates_upload_progress(tmp_path: Path):
    db = tmp_path / "migrate.db"
    Store(db)
    import sqlite3
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE jobs DROP COLUMN upload_progress")

    migrated = Store(db)
    with migrated._connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "upload_progress" in columns


def _create_pipeline_job(store: Store, job_id: str = "pipeline-1") -> None:
    store.create_job({
        "id": job_id,
        "source_type": "url",
        "source": "https://example.com/file.bin",
        "filename": "file.bin",
        "title": "file",
        "engine": "aria2",
        "options": {},
        "secret": {},
    })


def test_stage_claim_is_atomic_across_store_instances(tmp_path: Path):
    db = tmp_path / "claim.db"
    first_store = Store(db)
    _create_pipeline_job(first_store)
    first_store.ensure_stage("pipeline-1", "FETCH")
    second_store = Store(db)
    barrier = Barrier(2)

    def claim(store: Store, worker: str):
        barrier.wait()
        return store.claim_stage("FETCH", worker, lease_seconds=60)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda args: claim(*args), [(first_store, "w1"), (second_store, "w2")]))

    claimed = [result for result in results if result is not None]
    assert len(claimed) == 1
    assert claimed[0]["state"] == "RUNNING"

def test_expired_stage_is_reclaimed_with_new_fence(tmp_path: Path):
    db = tmp_path / "reclaim.db"
    store = Store(db)
    _create_pipeline_job(store)
    stage = store.ensure_stage("pipeline-1", "FETCH")
    first = store.claim_stage("FETCH", "worker-a", lease_seconds=60)
    assert first and first["id"] == stage["id"]

    with store._connect() as conn:
        conn.execute(
            "UPDATE job_stages SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",
            (stage["id"],),
        )

    reclaimed = Store(db).claim_stage("FETCH", "worker-b", lease_seconds=60)
    assert reclaimed
    assert reclaimed["claim_token"] != first["claim_token"]
    assert reclaimed["attempt"] == 2
    assert not store.finish_stage(stage["id"], first["claim_token"], success=True)
    assert store.finish_stage(stage["id"], reclaimed["claim_token"], success=True)
    assert store.list_stages("pipeline-1")[0]["state"] == "DONE"

def test_artifacts_are_durable_stage_outputs(tmp_path: Path):
    store = Store(tmp_path / "artifact.db")
    _create_pipeline_job(store)
    created = store.add_artifact(
        "pipeline-1",
        "FETCH",
        "primary",
        "/tmp/file.bin",
        size=123,
        sha256="abc123",
        metadata={"engine_task_id": "gid-1"},
    )

    assert created["role"] == "primary"
    assert created["metadata"]["engine_task_id"] == "gid-1"
    listed = Store(tmp_path / "artifact.db").list_artifacts("pipeline-1", "FETCH")
    assert listed == [created]