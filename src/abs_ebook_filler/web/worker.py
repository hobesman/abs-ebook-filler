"""Background download worker: runs queued picks in parallel, with a per-source limit.

Books are started in queue order, except that a book whose source is at its limit is passed over
for the next one that can start - so a waiting Anna's Archive download (1 at a time) never blocks
a torrent behind it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter

from ..core.service import Service

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, service: Service, concurrency: int | None = None):
        self.service = service
        self.concurrency = max(1, concurrency or service.s.total_download_slots)
        self._waiting: list[str] = []          # queue order
        self._source: dict[str, str] = {}      # item_id -> release source
        self._running: dict[str, str] = {}     # item_id -> release source
        self._changed = asyncio.Event()        # something was queued or finished
        self._idle = asyncio.Event()
        self._idle.set()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        # Resume anything left over from a previous run (State.active() is already in queue order).
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

    async def drain(self) -> None:
        """Wait until nothing is queued or running (used by tests)."""
        await self._idle.wait()

    def submit(self, item_id: str) -> bool:
        if item_id in self._running or item_id in self._source:
            return False
        row = self.service.state.get(item_id) or {}
        self._source[item_id] = str((row.get("release") or {}).get("source") or "")
        self._waiting.append(item_id)
        self._idle.clear()
        self._changed.set()
        return True

    def running_by_source(self) -> Counter:
        return Counter(self._running.values())

    def _claim(self) -> str | None:
        """Next waiting book whose source has a free slot. No awaits: atomic within the event loop."""
        busy = self.running_by_source()
        for i, item_id in enumerate(self._waiting):
            src = self._source[item_id]
            if busy[src] < self.service.s.source_limit(src):
                del self._waiting[i]
                self._running[item_id] = self._source.pop(item_id)
                return item_id
        return None

    async def _run(self, n: int) -> None:
        while True:
            item_id = self._claim()
            if item_id is None:
                self._changed.clear()
                await self._changed.wait()
                continue
            try:
                await self.service.process(item_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # already recorded in state by Service.process
                log.warning("worker %d: %s failed: %s", n, item_id, e)
            finally:
                self._running.pop(item_id, None)
                if not self._running and not self._waiting:
                    self._idle.set()
                self._changed.set()  # a slot freed up: let waiting workers re-check
