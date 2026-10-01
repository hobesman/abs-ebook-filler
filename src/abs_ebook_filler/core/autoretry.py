"""Auto-retry: while the toggle is on, put every failed download back in the queue - immediately when
it's switched on, then every AUTO_RETRY_MINUTES. Most failures (flaky mirrors, rate limits, a
Shelfmark hiccup) succeed on a plain retry.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from .service import Service

log = logging.getLogger(__name__)

# Failures a retry can't fix: the ebook is already there, or PATH_MAP points somewhere wrong.
PERMANENT_FAILURES = ("Refusing to overwrite existing file", "Audiobook folder not found")


class AutoRetry:
    def __init__(self, service: Service, submit: Callable[[str], Any],
                 interval_minutes: float | None = None,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 clock: Callable[[], float] = time.time):
        self.service = service
        self.submit = submit
        self.interval = 60 * (service.s.auto_retry_minutes if interval_minutes is None else interval_minutes)
        self._sleep = sleep
        self._clock = clock
        self._task: asyncio.Task | None = None

    # ---- state ---------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.service.state.get_pref("auto_retry", False))

    def last(self) -> dict[str, Any] | None:
        """{"at": epoch seconds, "count": books re-queued} for the latest run."""
        return self.service.state.get_pref("auto_retry_last")

    def next_due(self) -> float | None:
        if not self.enabled:
            return None
        last = self.last()
        return (last["at"] + self.interval) if last else self._clock()

    def due(self) -> bool:
        nxt = self.next_due()
        return nxt is not None and not self.service.paused() and self._clock() >= nxt

    # ---- actions -------------------------------------------------------------------
    def run_once(self) -> int:
        """Re-queue every failed download that a retry could fix. Returns how many."""
        count = 0
        for row in self.service.state.list(status="failed", limit=100_000):
            if not row.get("release") or (row.get("message") or "").startswith(PERMANENT_FAILURES):
                continue
            self.service.enqueue(row["item_id"], row["release"])
            self.submit(row["item_id"])
            count += 1
        self.service.state.set_pref("auto_retry_last", {"at": self._clock(), "count": count})
        if count:
            log.info("Auto-retry re-queued %d failed download(s)", count)
        return count

    def enable(self) -> int:
        self.service.state.set_pref("auto_retry", True)
        return self.run_once()  # "once when turned on"

    def disable(self) -> None:
        self.service.state.set_pref("auto_retry", False)

    # ---- background loop -----------------------------------------------------------
    async def loop(self, check_every: float = 60.0) -> None:
        while True:
            try:
                if self.due():
                    self.run_once()
            except Exception:  # never let the loop die
                log.exception("Auto-retry run failed")
            await self._sleep(check_every)

    def start(self) -> None:
        self._task = asyncio.create_task(self.loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
