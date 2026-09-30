"""Background download worker: processes queued picks one (or N) at a time."""

from __future__ import annotations

import asyncio
import logging

from ..core.service import Service

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, service: Service, concurrency: int = 1):
        self.service = service
        self.concurrency = max(1, concurrency)
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._pending: set[str] = set()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        # Resume anything left over from a previous run.
        for row in self.service.state.active():
            if row.get("release"):
                self.service.state.update(row["item_id"], status="queued", message="Resumed after restart")
                self.submit(row["item_id"])
            else:
                self.service.state.update(row["item_id"], status="missing", message="")
        self._tasks = [asyncio.create_task(self._run(i)) for i in range(self.concurrency)]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def submit(self, item_id: str) -> bool:
        if item_id in self._pending:
            return False
        self._pending.add(item_id)
        self.queue.put_nowait(item_id)
        return True

    async def _run(self, n: int) -> None:
        while True:
            item_id = await self.queue.get()
            try:
                await self.service.process(item_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # already recorded in state by Service.process
                log.warning("worker %d: %s failed: %s", n, item_id, e)
            finally:
                self._pending.discard(item_id)
                self.queue.task_done()
