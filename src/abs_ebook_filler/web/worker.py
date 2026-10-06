"""Background download worker: runs queued picks in parallel, with a per-source limit.

Books are started in queue order, except that a book whose source is at its limit is passed over
for the next one that can start - so a waiting Anna's Archive download (1 at a time) never blocks
a torrent behind it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter

from ..core.annas import aa_md5, is_aa_release
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
        self._wake: asyncio.TimerHandle | None = None  # fires when a pause ends
        self._jobs: dict[str, asyncio.Task] = {}       # item_id -> its running Service.process task
        self._release: dict[str, dict] = {}            # item_id -> chosen release (while waiting)
        self._aa_blocked: set[str] = set()             # held back waiting for a fast-download slot
        self._aa_wake: asyncio.TimerHandle | None = None
        service.aa.on_change(self._changed.set)        # new fast-download numbers: re-check the queue

    async def start(self) -> None:
        # Resume anything left over from a previous run (State.active() is already in queue order).
        for row in self.service.state.active():
            if row.get("release"):
                self.service.state.update(row["item_id"], status="queued", message="Resumed after restart")
                self.submit(row["item_id"])
            else:
                self.service.state.update(row["item_id"], status="missing", message="")
        if self.service.paused():  # a pause set before a restart still applies
            self._schedule_wake()
        self._tasks = [asyncio.create_task(self._run(i)) for i in range(self.concurrency)]

    async def stop(self) -> None:
        if self._wake:
            self._wake.cancel()
        if self._aa_wake:
            self._aa_wake.cancel()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ---- pause / resume ------------------------------------------------------------
    def pause(self, minutes: float) -> None:
        """Start no new downloads for ``minutes``; running ones hold off contacting Shelfmark."""
        self.service.pause(minutes)
        self._schedule_wake()

    def resume(self) -> None:
        self.service.resume()
        if self._wake:
            self._wake.cancel()
            self._wake = None
        self._changed.set()

    def _schedule_wake(self) -> None:
        if self._wake:
            self._wake.cancel()
        self._wake = asyncio.get_running_loop().call_later(
            self.service.pause_left() + 0.5, self._changed.set)

    # ---- queue order ---------------------------------------------------------------
    def claimed_ids(self) -> set[str]:
        """Books a worker has started (downloading, or waiting in Shelfmark's own queue)."""
        return set(self._running)

    def reorder(self, item_ids: list[str]) -> None:
        """Put these books in this order, within the queue positions they already occupy (everything
        else stays exactly where it is) - matches State.reorder_queue, and works for one page."""
        wanted = set(item_ids)
        slots = [i for i, item_id in enumerate(self._waiting) if item_id in wanted]
        present = set(self._waiting[i] for i in slots)
        ordered = [i for i in dict.fromkeys(item_ids) if i in present]
        for slot, item_id in zip(slots, ordered):
            self._waiting[slot] = item_id

    def move(self, item_id: str, front: bool) -> None:
        """Front (process next) or end of the waiting list."""
        if item_id in self._source:
            self._waiting.remove(item_id)
            self._waiting.insert(0, item_id) if front else self._waiting.append(item_id)

    def blocked_ids(self) -> set[str]:
        """Books currently held back waiting for an Anna's Archive fast-download slot."""
        return set(self._aa_blocked)

    async def drain(self) -> None:
        """Wait until nothing is queued or running (used by tests)."""
        await self._idle.wait()

    def submit(self, item_id: str) -> bool:
        if item_id in self._running or item_id in self._source:
            return False
        row = self.service.state.get(item_id) or {}
        self._source[item_id] = str((row.get("release") or {}).get("source") or "")
        self._release[item_id] = row.get("release") or {}
        self._waiting.append(item_id)
        self._idle.clear()
        self._changed.set()
        return True

    def running_by_source(self) -> Counter:
        return Counter(self._running.values())

    def wake(self) -> None:
        self._changed.set()

    def _claim(self) -> str | None:
        """Next waiting book that can start: its source has a free slot and, for Anna's Archive
        Direct Downloads with "wait for a fast download slot" on, a fast-download slot is free.
        Books that can't start are passed over, so torrents never queue behind them.
        No awaits: atomic within the event loop."""
        if self.service.paused():
            return None
        # Everything a pass needs is read once up front; per book it's only in-memory checks, and
        # nothing is written to the database (this runs on every click and every finished download,
        # with thousands of books queued).
        busy = self.running_by_source()
        limits: dict[str, int] = {}
        blocked = self.service.aa_blocked_checker()
        any_blocked = False
        for i, item_id in enumerate(self._waiting):
            src = self._source[item_id]
            limit = limits.get(src)
            if limit is None:
                limit = limits[src] = self.service.s.source_limit(src)
            if busy[src] >= limit:
                continue
            release = self._release.get(item_id)
            if blocked(release):
                self._aa_blocked.add(item_id)  # shown as "waiting for a fast download slot"
                any_blocked = True
                continue
            self._aa_blocked.discard(item_id)
            del self._waiting[i]
            self._running[item_id] = self._source.pop(item_id)
            self._release.pop(item_id, None)
            if is_aa_release(release):
                self.service.aa.reserve(aa_md5(release))  # counts until Anna's Archive confirms it
            return item_id
        if any_blocked:
            self._schedule_aa_wake()
        else:
            self._aa_blocked.clear()  # nothing is held back any more
        return None

    def _schedule_aa_wake(self) -> None:
        """Wake up when a slot is certain to have freed (the periodic check usually notices sooner)."""
        if self._aa_wake:
            return
        nxt = self.service.aa.next_free_at()
        if nxt:
            delay = max(1.0, nxt - time.time() + 5)
            self._aa_wake = asyncio.get_running_loop().call_later(delay, self._aa_timer)

    def _aa_timer(self) -> None:
        self._aa_wake = None
        self._changed.set()

    def next_blocked_aa_md5(self) -> str | None:
        """md5 of the first book waiting for a fast-download slot (used to check the quota)."""
        blocked = self.service.aa_blocked_checker()
        for item_id in self._waiting:
            release = self._release.get(item_id)
            if blocked(release):
                return aa_md5(release)
        return None

    async def _run(self, n: int) -> None:
        while True:
            item_id = self._claim()
            if item_id is None:
                self._changed.clear()
                await self._changed.wait()
                continue
            # Each book runs as its own task so it can be cancelled without stopping this worker.
            job = asyncio.create_task(self.service.process(item_id))
            self._jobs[item_id] = job
            try:
                await job
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():  # the worker itself is stopping
                    job.cancel()
                    raise
                log.info("worker %d: %s cancelled", n, item_id)  # cancelled by the user
            except Exception as e:  # already recorded in state by Service.process
                log.warning("worker %d: %s failed: %s", n, item_id, e)
            finally:
                self._jobs.pop(item_id, None)
                self._running.pop(item_id, None)
                if not self._running and not self._waiting:
                    self._idle.set()
                self._changed.set()  # a slot freed up: let waiting workers re-check

    # ---- cancel --------------------------------------------------------------------
    def cancel(self, item_id: str) -> str | None:
        """Take a book out of the queue, or stop its download if it already started.

        Returns "waiting", "running", or None if the worker didn't have it.
        """
        if item_id in self._source:  # still waiting in our queue
            self._waiting.remove(item_id)
            del self._source[item_id]
            self._release.pop(item_id, None)
            self._aa_blocked.discard(item_id)
            if not self._running and not self._waiting:
                self._idle.set()
            return "waiting"
        job = self._jobs.get(item_id)
        if job and not job.done():
            job.cancel()
            return "running"
        return None
