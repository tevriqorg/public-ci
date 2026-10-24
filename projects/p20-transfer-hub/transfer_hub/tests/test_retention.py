from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os

from transfer_hub.db import Store
from transfer_hub.domain import JobState
from transfer_hub.retention import RETENTION_SECONDS, sweep_jobs, sweep_orphan_spool
from transfer_hub.service import TransferService
from transfer_hub.config import Settings
from transfer_hub.watchd import DEFAULT_TASKS


def setup(tmp_path):
    token = tmp_path / "token"
    token.write_text("test")
    store = Store(tmp_path / "transfer.db")
    settings = Settings(tmp_path, "127.0.0.1", 0, token, tmp_path / "secrets", "http://127.0.0.1:9", tmp_path / "none")
    return store, TransferService(settings, store)


def test_one_policy_for_manual_and_watcher(tmp_path):
    store, service = setup(tmp_path)
    manual = service.create_url_job({"source": "https://example.org/file", "ttl_seconds": 604800})
    assert manual["options"]["ttl_seconds"] == RETENTION_SECONDS
    assert manual["cleanup_at"] is None
    for default in DEFAULT_TASKS:
        assert default["config"]["ttl_seconds"] == RETENTION_SECONDS
    store.update(manual["id"], state="VERIFYING", created_at="2000-01-01T00:00:00+00:00")
    ready = store.transition(manual["id"], JobState.READY)
    assert 47.9 < (datetime.fromisoformat(ready["cleanup_at"]) - datetime.now(timezone.utc)).total_seconds() / 3600 <= 48
    assert sweep_jobs(store, tmp_path)["ready"] == 0


def test_ready_expired_while_hub_asleep_is_cleaned_once(tmp_path):
    store, service = setup(tmp_path)
    job = service.create_url_job({"source": "https://example.org/file"})
    store.update(job["id"], state="VERIFYING")
    store.transition(job["id"], JobState.READY)
    work = tmp_path / "jobs" / job["id"]
    work.mkdir(parents=True)
    (work / "download.zip").write_bytes(b"payload")
    store.update(job["id"], cleanup_at="2000-01-01T00:00:00+00:00")
    assert sweep_jobs(store, tmp_path)["ready"] == 1
    assert store.get_job(job["id"])["state"] == "DONE"
    assert not work.exists()
    assert sweep_jobs(store, tmp_path)["ready"] == 0


def test_crash_recovery_of_claimed_cleanup(tmp_path):
    store, service = setup(tmp_path)
    job = service.create_url_job({"source": "https://example.org/file"})
    store.update(job["id"], state="READY", cleanup_at="2000-01-01T00:00:00+00:00")
    assert store.claim_expired_ready(job["id"], datetime.now(timezone.utc).isoformat())
    assert store.get_job(job["id"])["state"] == "CLEANUP"
    assert sweep_jobs(Store(tmp_path / "transfer.db"), tmp_path)["ready"] == 1
    assert store.get_job(job["id"])["state"] == "DONE"


def test_failed_retains_metadata_and_vps_local_source(tmp_path):
    store, service = setup(tmp_path)
    data = tmp_path / "source-input.apk"
    data.write_bytes(b"VPS source")
    job = service.create_local_job(data, "source-input.apk", {"local_origin": "vps"})
    store.update(job["id"], state="VERIFYING")
    failed = store.transition(job["id"], JobState.FAILED)
    assert failed["cleanup_at"]
    work = tmp_path / "jobs" / job["id"]
    work.mkdir(parents=True)
    (work / "artifact.zip").write_bytes(b"payload")
    store.update(job["id"], cleanup_at="2000-01-01T00:00:00+00:00")
    assert sweep_jobs(store, tmp_path)["failed"] == 1
    assert data.read_bytes() == b"VPS source"
    assert not work.exists()
    assert store.get_job(job["id"])["state"] == "FAILED"
    assert store.get_job(job["id"])["cleanup_at"] is None


def test_active_jobs_never_cleaned(tmp_path):
    store, service = setup(tmp_path)
    job = service.create_url_job({"source": "https://example.org/file"})
    work = tmp_path / "jobs" / job["id"]
    work.mkdir(parents=True)
    (work / "keep").write_bytes(b"keep")
    store.update(job["id"], cleanup_at="2000-01-01T00:00:00+00:00")
    sweep_jobs(store, tmp_path)
    assert (work / "keep").exists()


def test_spool_orphans_only_after_48h_and_unreferenced(tmp_path):
    store, _service = setup(tmp_path)
    spool = tmp_path / "spool"
    spool.mkdir()
    old = spool / "untracked.bin"
    active = spool / "referenced.bin"
    fresh = spool / "new.bin"
    old.write_bytes(b"old")
    active.write_bytes(b"active")
    fresh.write_bytes(b"new")
    state = {"status": {}, "paths": {"file_set": str(active)}, "idempotency": {}}
    (spool / ".p20-flash-state.json").write_text(json.dumps(state))
    now = datetime.now(timezone.utc).timestamp()
    for p in (old, active):
        os.utime(p, (now - 173000, now - 173000))
    assert sweep_orphan_spool(spool, store, now_ts=now) == 1
    assert not old.exists() and active.exists() and fresh.exists()
    (spool / ".p20-flash-state.json").write_text("invalid JSON")
    ancient = spool / "ancient.bin"
    ancient.write_bytes(b"old")
    os.utime(ancient, (now - 174000, now - 174000))
    assert sweep_orphan_spool(spool, store, now_ts=now) == 0
    assert ancient.exists()


def test_spool_does_not_run_when_job_is_active(tmp_path):
    store, service = setup(tmp_path)
    job = service.create_url_job({"source": "https://example.org/file"})
    # Queued jobs have a READY stage in the pipeline.
    spool = tmp_path / "spool"
    spool.mkdir()
    old = spool / "old.bin"
    old.write_bytes(b"x")
    now = datetime.now(timezone.utc).timestamp()
    os.utime(old, (now - 200000, now - 200000))
    (spool / ".p20-flash-state.json").write_text(json.dumps({"paths": {}}))
    assert store.active_stage_count() > 0
    assert sweep_orphan_spool(spool, store, now_ts=now) == 0
    assert old.exists()


def test_watchd_real_startup_and_retention_tick(tmp_path, monkeypatch, capsys):
    """Exercise main(), not just Store/retention helpers: catches missing imports."""
    import transfer_hub.watchd as watchd
    monkeypatch.setenv("P20_TRANSFER_DATA", str(tmp_path))
    monkeypatch.setenv("P20_WATCHER_HUB_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("P20_QQ_SPOOL_ROOT", raising=False)
    monkeypatch.setattr(watchd.WatchRunner, "run", lambda *_args: {"outcome": "SKIPPED"})
    monkeypatch.setattr(watchd.ReadyNotifier, "scan", lambda *_args: [])
    def stop_after_first_tick(_seconds):
        raise StopIteration("one watchd iteration complete")
    monkeypatch.setattr(watchd, "sleep_until_next_tick", stop_after_first_tick)
    import pytest
    with pytest.raises(StopIteration, match="one watchd iteration complete"):
        watchd.main()
    out = capsys.readouterr().out
    assert '"event": "watchd.started"' in out
    from transfer_hub.db import Store
    store = Store(tmp_path / "transfer.db")
    assert len(store.list_watch_tasks()) == 2
    assert {t["config"]["ttl_seconds"] for t in store.list_watch_tasks()} == {172800}
