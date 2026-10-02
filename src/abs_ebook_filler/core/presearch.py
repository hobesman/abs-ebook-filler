"""Background pre-search: search the next N missing books ahead of time so they open instantly.

Runs strictly one search at a time with a pause between books, gives way to interactive searches,
and waits out Anna's Archive rate-limit cooldowns (retrying the book) so saved results are complete.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable

from .ratelimit import is_rate_limit, rate_limit_wait  # noqa: F401  (re-exported for the web app)
from .service import Service
from .shelfmark_client import ShelfmarkError

log = logging.getLogger(__name__)

# Books that already have (or are getting) an ebook: never worth pre-searching.
NOT_SEARCHABLE = ("queued", "downloading", "done")


@dataclass
class PreSearchStatus:
    running: bool = False
    total: int = 0
    done: int = 0
    failed: int = 0
    partial: int = 0  # finished, but a source still failed after retries
    current: str = ""
    cooldown_until: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cooldown_left"] = max(0, round(self.cooldown_until - time.time()))
        return d


class PreSearcher:
    def __init__(self, service: Service, is_busy: Callable[[], bool] = lambda: False,
                 delay: float | None = None, max_retries: int = 2,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep):
        self.service = service
        self.is_busy = is_busy
        self.delay = service.s.presearch_delay if delay is None else delay
        self.max_retries = max_retries
        self._sleep = sleep
        self.status = PreSearchStatus()
        self._task: asyncio.Task | None = None

    # ---- control -----------------------------------------------------------------
    def targets(self, count: int, status: str | None = "missing", library_id: str | None = None,
                q: str | None = None) -> list[dict[str, Any]]:
        """The next ``count`` books of a Books-page view (same filters, same order) that still need an
        ebook and don't already have complete saved results. ``status=None`` means "All"."""
        ready = self.service.state.searched_ids(self.service.s.search_cache_seconds, complete_only=True)
        rows = self.service.state.list(status=status or None, library_id=library_id or None, q=q or None,
                                       limit=100_000)
        return [r for r in rows
                if r["status"] not in NOT_SEARCHABLE and r["item_id"] not in ready][:max(0, count)]

    def start(self, count: int, status: str | None = "missing", library_id: str | None = None,
              q: str | None = None) -> bool:
        if self.status.running:
            return False
        targets = self.targets(count, status, library_id, q)
        self.status = PreSearchStatus(running=bool(targets), total=len(targets), started_at=time.time(),
                                      message="" if targets else "Every book shown already has results ready.")
        if targets:
            self._task = asyncio.create_task(self.run(targets))
        return bool(targets)

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def wait(self) -> None:
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)

    # ---- work --------------------------------------------------------------------
    async def run(self, targets: list[dict[str, Any]]) -> PreSearchStatus:
        st = self.status
        st.running, st.total = True, len(targets)
        try:
            for i, row in enumerate(targets):
                while self.is_busy():  # the person clicking around comes first
                    st.current = "(paused while you search)"
                    await self._sleep(1)
                st.current = row["clean_title"]
                await self._search_one(row["item_id"])
                st.done += 1
                if i < len(targets) - 1 and self.delay:
                    await self._sleep(self.delay)
            st.message = f"Finished: {st.done} searched"
        except asyncio.CancelledError:
            st.message = f"Stopped after {st.done} of {st.total}"
            raise
        finally:
            st.running, st.current, st.cooldown_until = False, "", 0.0
            st.finished_at = time.time()
        return st

    async def _search_one(self, item_id: str) -> None:
        st = self.status
        for attempt in range(self.max_retries + 1):
            try:
                res = await self.service.search_all(item_id)
                msgs = res.warnings
            except ShelfmarkError as e:
                res, msgs = None, [str(e)]
            except KeyError:
                return  # book vanished (rescan) - nothing to do
            wait = rate_limit_wait(msgs)
            if wait is not None and attempt < self.max_retries:
                st.cooldown_until = time.time() + wait + 5
                await self._sleep(wait + 5)
                st.cooldown_until = 0.0
                continue
            if res is None:
                st.failed += 1
                log.warning("Pre-search failed for %s: %s", item_id, msgs[0] if msgs else "?")
            elif msgs:
                st.partial += 1
            return
