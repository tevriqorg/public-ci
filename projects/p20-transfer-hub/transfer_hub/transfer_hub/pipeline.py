from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from typing import Any

from .db import Store


class StagePaused(RuntimeError):
    pass


class StageCancelled(RuntimeError):
    pass


StageHandler = Callable[[dict[str, Any]], None]
StageErrorHandler = Callable[[str, dict[str, Any], BaseException], None]


class PipelineScheduler:
    """Lease-backed in-process worker pools over the durable SQLite stage ledger."""

    def __init__(self, store: Store, *, poll_seconds: float, lease_seconds: int,
                 on_error: StageErrorHandler) -> None:
        self.store = store
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self.on_error = on_error
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    def add_pool(self, stage: str, concurrency: int, handler: StageHandler) -> None:
        for index in range(max(1, concurrency)):
            worker_id = f"{stage.lower()}-{index + 1}-{uuid.uuid4().hex[:8]}"
            self._tasks.append(asyncio.create_task(self._worker(stage, worker_id, handler)))

    async def stop(self) -> None:
        self._stop.set()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _worker(self, stage: str, worker_id: str, handler: StageHandler) -> None:
        while not self._stop.is_set():
            claim = await asyncio.to_thread(
                self.store.claim_stage, stage, worker_id, self.lease_seconds
            )
            if not claim:
                await asyncio.sleep(self.poll_seconds)
                continue
            await self._run_claim(stage, claim, handler)

    async def _run_claim(self, stage: str, claim: dict[str, Any], handler: StageHandler) -> None:
        stage_id = int(claim["id"])
        token = str(claim["claim_token"])
        work = asyncio.create_task(asyncio.to_thread(handler, claim))
        heartbeat = asyncio.create_task(self._heartbeat(stage_id, token))
        try:
            await work
        except StagePaused:
            await asyncio.to_thread(self.store.pause_stage, stage_id, token)
        except StageCancelled:
            await asyncio.to_thread(self.store.cancel_active_stages, claim["job_id"])
        except BaseException as exc:
            await asyncio.to_thread(
                self.store.finish_stage, stage_id, token, success=False, error=str(exc)[:1000]
            )
            await asyncio.to_thread(self.on_error, stage, claim, exc)
        else:
            await asyncio.to_thread(self.store.finish_stage, stage_id, token, success=True)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat(self, stage_id: int, token: str) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            ok = await asyncio.to_thread(
                self.store.extend_stage_lease, stage_id, token, self.lease_seconds
            )
            if not ok:
                return
