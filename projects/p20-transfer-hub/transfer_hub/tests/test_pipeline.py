import asyncio
import threading
import time
from pathlib import Path

from transfer_hub.db import Store
from transfer_hub.pipeline import PipelineScheduler


def _job(store: Store) -> None:
    store.create_job({
        "id": "j1", "source_type": "url", "source": "https://example.test/a",
        "filename": "a.bin", "title": "a", "engine": "aria2",
        "options": {}, "secret": {},
    })
    store.ensure_stage("j1", "FETCH")


def test_scheduler_heartbeat_prevents_lease_steal(tmp_path: Path):
    async def scenario():
        store = Store(tmp_path / "pipeline.db")
        _job(store)
        started = threading.Event()

        def handler(_claim):
            started.set()
            time.sleep(2.0)

        scheduler = PipelineScheduler(store, poll_seconds=0.02, lease_seconds=1, on_error=lambda *_: None)
        scheduler.add_pool("FETCH", 1, handler)
        assert await asyncio.to_thread(started.wait, 1.0)
        await asyncio.sleep(1.2)
        stolen = Store(tmp_path / "pipeline.db").claim_stage("FETCH", "intruder", lease_seconds=1)
        assert stolen is None
        await asyncio.sleep(1.0)
        await scheduler.stop()
        assert store.latest_stage("j1", "FETCH")["state"] == "DONE"

    asyncio.run(scenario())
