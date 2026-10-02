"""Anna's Archive fast-download quota, tracked with the membership key.

How Anna's Archive counts (from its server code: allthethings/dyn/views.py ``api_md5_fast_download``
and allthethings/utils.py ``get_account_fast_download_info``):

* ``GET {base}/dyn/api/fast_download.json?md5=<md5>&key=<key>`` returns
  ``account_fast_download_info = {downloads_left, downloads_per_day, recently_downloaded_md5s}``.
* The limit is a rolling 18-hour window: each fast download counts for 18 h after it happened.
* Asking for an md5 that's already in ``recently_downloaded_md5s`` is free (not counted again).
* For any other md5: with ``downloads_left == 0`` it answers 429 ``{"error": "No downloads left"}``
  and counts nothing; with ``downloads_left > 0`` it **counts a download** for that md5.

So checks only ever use an md5 that is surely still inside the window (free), or the md5 of the next
book we actually want to download - if that uses up a slot, it's the slot that book was about to
use anyway (and Shelfmark's own request for it is then free).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

import httpx

from .state import State

log = logging.getLogger(__name__)

WINDOW = 18 * 3600        # Anna's Archive counts fast downloads over the last 18 hours
SAFETY = 30 * 60          # only probe with md5s that are this far from leaving the window
PENDING_TTL = 45 * 60     # a book we sent is assumed to use a slot until it shows up (or this passes)
PREF = "aa_quota"


def is_aa_release(release: dict[str, Any] | None) -> bool:
    """Direct Download releases served by Anna's Archive (the ones that use fast-download slots)."""
    if not release or str(release.get("source") or "") != "direct_download":
        return False
    provider = ((release.get("extra") or {}).get("direct_download_provider") or "annas_archive")
    return str(provider).lower() == "annas_archive"


def aa_md5(release: dict[str, Any] | None) -> str:
    return str((release or {}).get("source_id") or "").strip().lower()


class AnnasQuota:
    def __init__(self, state: State, key: str, base_url: str, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None, clock: Callable[[], float] = time.time):
        self.state = state
        self.key = key.strip()
        self._clock = clock
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport,
                                       follow_redirects=True)
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[], Any]] = []

    @property
    def configured(self) -> bool:
        return bool(self.key)

    async def aclose(self) -> None:
        await self._http.aclose()

    def on_change(self, fn: Callable[[], Any]) -> None:
        self._listeners.append(fn)

    def _changed(self) -> None:
        for fn in self._listeners:
            try:
                fn()
            except Exception:
                log.exception("quota listener failed")

    # ---- model -------------------------------------------------------------------
    # recent: md5 -> {"lo": earliest possible download time, "hi": latest possible download time}
    # pending: md5 -> time we handed it to Shelfmark (not yet seen in Anna's Archive's list)
    def _data(self) -> dict[str, Any]:
        d = dict(self.state.get_pref(PREF) or {})
        d.setdefault("per_day", None)
        d.setdefault("checked_at", 0.0)
        d.setdefault("recent", {})
        d.setdefault("pending", {})
        d.setdefault("zero_until", 0.0)
        d.setdefault("error", "")
        now = self._clock()
        d["recent"] = {m: e for m, e in d["recent"].items() if now < e["hi"] + WINDOW}
        d["pending"] = {m: t for m, t in d["pending"].items()
                        if m not in d["recent"] and now - t < PENDING_TTL}
        return d

    def _save(self, d: dict[str, Any]) -> None:
        self.state.set_pref(PREF, d)

    def left(self) -> int | None:
        """Fast downloads left right now (conservative estimate between checks); None if unknown."""
        d = self._data()
        if self._clock() < d["zero_until"]:  # Anna's Archive just said "No downloads left"
            return 0
        if d["per_day"] is None:
            return None
        return max(0, int(d["per_day"]) - len(d["recent"]) - len(d["pending"]))

    def slot_available(self, md5: str) -> bool:
        """Would sending this md5 to Shelfmark get a fast download right now? (unknown -> yes)"""
        if not self.configured:
            return True
        d = self._data()
        if md5 in d["recent"] or md5 in d["pending"]:
            return True  # already counted: asking again is free
        left = self.left()
        return left is None or left > 0

    def reserve(self, md5: str) -> None:
        """A book was just handed to Shelfmark: count its slot until Anna's Archive's list shows it."""
        if not self.configured or not md5:
            return
        d = self._data()
        if md5 not in d["recent"]:
            d["pending"][md5] = self._clock()
            self._save(d)

    def release(self, md5: str) -> None:
        """A book handed to Shelfmark has finished (or failed/was cancelled): stop counting it as pending.

        If Shelfmark really used a fast download for it, Anna's Archive's list says so at the next
        check and it's counted from there; if it didn't (slow mirror, no fast copy, failure), the slot
        was never used and shouldn't stay reserved.
        """
        if not self.configured or not md5:
            return
        d = self._data()
        if d["pending"].pop(md5, None) is not None:
            self._save(d)
            self._changed()

    def next_free_at(self) -> float | None:
        """When a slot is certain to have freed up (None if one is free or nothing is known)."""
        if (self.left() or 0) > 0:
            return None
        d = self._data()
        times = [e["hi"] + WINDOW for e in d["recent"].values()]
        return min(times) if times else None

    def status(self) -> dict[str, Any]:
        d = self._data()
        return {"configured": self.configured, "left": self.left(), "per_day": d["per_day"],
                "checked_at": d["checked_at"], "error": d["error"], "next_free_at": self.next_free_at()}

    def _safe_probe_md5(self, d: dict[str, Any]) -> str | None:
        """Newest md5 that is certainly still inside Anna's Archive's window (probing it is free)."""
        now = self._clock()
        safe = [(e["lo"], m) for m, e in d["recent"].items() if now < e["lo"] + WINDOW - SAFETY]
        return max(safe)[1] if safe else None

    # ---- checking ----------------------------------------------------------------
    async def refresh(self, next_wanted: str | None = None, downloaded: str | None = None) -> dict[str, Any]:
        """Ask Anna's Archive for the current numbers, without spending a fast download.

        Probe md5, in order of preference: one certainly still in the window (free); the newest book
        Shelfmark is working on right now (free once Shelfmark fetched it; otherwise it takes the slot
        that book is about to use); ``next_wanted``, the next book waiting for a slot (same idea); and,
        only while the count is still unknown, ``downloaded`` - a book that just finished. That last one
        is free if Shelfmark used a fast download for it, but would spend a slot if it used a slow
        mirror, so it's limited to the very first check.
        """
        if not self.configured:
            return self.status()
        async with self._lock:
            d = self._data()
            newest_pending = max(d["pending"], key=d["pending"].get) if d["pending"] else ""
            first_check = (downloaded or "").lower() if d["per_day"] is None else ""
            md5 = (self._safe_probe_md5(d) or newest_pending or (next_wanted or "").lower()
                   or first_check or None)
            if not md5:
                if not d["recent"] and not d["pending"] and d["per_day"] is not None:
                    d["zero_until"] = 0.0  # nothing in the window: the full allowance is available
                    self._save(d)
                return self.status()
            try:
                r = await self._http.get("/dyn/api/fast_download.json", params={"md5": md5, "key": self.key})
                body = r.json() if r.content else {}
            except (httpx.HTTPError, ValueError) as e:
                d["error"] = f"Couldn't check Anna's Archive ({type(e).__name__})"
                self._save(d)
                return self.status()
            info = body.get("account_fast_download_info") if isinstance(body, dict) else None
            error = str((body or {}).get("error") or "") if isinstance(body, dict) else ""
            now = self._clock()
            if r.status_code in (200, 204) and isinstance(info, dict):
                self._apply(d, info, now)
                if md5 not in d["recent"]:
                    # The probe itself just counted a download for the next wanted book (the numbers
                    # Anna's Archive returns are from before it): record it.
                    d["recent"][md5] = {"lo": now - 60, "hi": now}
                    d["pending"].pop(md5, None)
            elif r.status_code == 429 and error.lower() == "no downloads left":
                # Only possible for an md5 outside the window, and nothing was counted.
                d["recent"].pop(md5, None)
                d["zero_until"] = now + 600
                d["checked_at"], d["error"] = now, ""
            elif r.status_code == 401:
                d["error"] = "Anna's Archive rejected the key (check ANNAS_ARCHIVE_KEY)"
            elif r.status_code == 403:
                d["error"] = "Anna's Archive says this key has no active membership"
            else:
                d["error"] = f"Anna's Archive answered {r.status_code}: {error or r.text[:120]}"
            self._save(d)
        self._changed()
        return self.status()

    def _apply(self, d: dict[str, Any], info: dict[str, Any], now: float) -> None:
        prev_check = d["checked_at"] if d["checked_at"] and d["checked_at"] > now - WINDOW else None
        recent: dict[str, Any] = {}
        for md5 in info.get("recently_downloaded_md5s") or []:
            md5 = str(md5).lower()
            if md5 in d["recent"]:
                recent[md5] = d["recent"][md5]
            elif md5 in d["pending"]:  # one of ours: downloaded after we sent it
                recent[md5] = {"lo": d["pending"][md5], "hi": now}
            else:  # appeared since the last check (or before we ever looked)
                recent[md5] = {"lo": prev_check or now - WINDOW, "hi": now}
        d["recent"] = recent
        d["pending"] = {m: t for m, t in d["pending"].items() if m not in recent}
        d["per_day"] = int(info.get("downloads_per_day") or 0)
        d["zero_until"] = 0.0
        d["checked_at"], d["error"] = now, ""
