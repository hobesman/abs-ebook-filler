"""Orchestration shared by the CLI and the web UI."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable

from . import placer
from .abs_client import ABSClient, has_ebook, is_missing_ebook, item_to_book
from .config import Settings
from .matching import build_query, primary_author, score
from .models import Book, Candidate, SearchResult
from .annas import AnnasQuota, aa_md5, is_aa_release
from .ratelimit import rate_limit_wait
from .shelfmark_client import (QUEUED_STATES, ShelfmarkClient, ShelfmarkError, ShelfmarkUnreachable,
                               TaskLostError, release_format)
from .state import State, source_key

log = logging.getLogger(__name__)

ProgressCb = Callable[[str, "float | None", str], Any]


class Service:
    def __init__(self, settings: Settings, state: State | None = None,
                 abs_client: ABSClient | None = None, shelfmark: ShelfmarkClient | None = None):
        self.s = settings
        self.state = state or State(Path(settings.data_dir) / "state.db")
        self.abs = abs_client or ABSClient(settings.abs_url, settings.abs_token, settings.http_timeout)
        self._sleep = asyncio.sleep  # swapped out in tests to skip real cooldown waits
        self._paused_until = float(self.state.get_pref("paused_until", 0) or 0)
        self.aa = AnnasQuota(self.state, settings.annas_archive_key, settings.annas_archive_url,
                             settings.http_timeout)
        self._aa_wait: bool | None = None
        self.sm = shelfmark or ShelfmarkClient(
            settings.shelfmark_url, settings.shelfmark_api_key, settings.http_timeout,
            search_timeout=settings.search_timeout, search_concurrency=settings.search_concurrency)

    async def aclose(self) -> None:
        await self.abs.aclose()
        await self.sm.aclose()
        await self.aa.aclose()
        self.state.close()

    # ---- Anna's Archive fast-download slots --------------------------------------
    def aa_wait_enabled(self) -> bool:
        """Hold Anna's Archive Direct Downloads until a fast-download slot is free (Activity switch)."""
        if self._aa_wait is None:  # read once, then kept in memory (checked for every queued book)
            self._aa_wait = bool(self.state.get_pref("aa_wait_for_fast", False))
        return self.aa.configured and self._aa_wait

    def set_aa_wait(self, enabled: bool) -> None:
        self._aa_wait = bool(enabled)
        self.state.set_pref("aa_wait_for_fast", bool(enabled))

    def aa_blocked(self, release: dict[str, Any] | None) -> bool:
        """True if this release should wait for a fast-download slot right now."""
        return self.aa_blocked_checker()(release)

    def aa_blocked_checker(self) -> Callable[[dict[str, Any] | None], bool]:
        """A cheap per-release test for one pass over the queue (settings read once, not per book)."""
        if not self.aa_wait_enabled():
            return lambda release: False
        md5_blocked = self.aa.blocked_md5s_checker()
        return lambda release: is_aa_release(release) and md5_blocked(aa_md5(release))

    # ---- scanning ----------------------------------------------------------------
    async def scan(self) -> dict[str, int]:
        books = await self.abs.missing_ebooks(self.s.library_ids or None)
        return self.state.sync_missing(books)

    async def load_item(self, item_id: str) -> Book:
        item = await self.abs.get_item(item_id)
        book = item_to_book(item)
        if is_missing_ebook(item):
            self.state.add_book(book)
        return book

    # ---- searching ---------------------------------------------------------------
    def default_query(self, row: dict[str, Any]) -> str:
        return row.get("query") or build_query(row["clean_title"], row["author"])

    async def search(self, item_id: str, query: str | None = None) -> list[Candidate]:
        """Search and return what should be shown: enabled sources only, top N."""
        return self.select((await self.search_all(item_id, query)).cands)[0]

    def select(self, cands: list[Candidate]) -> tuple[list[Candidate], int]:
        """Apply min score, disabled sources and the candidate limit.

        Returns (shown, hidden_by_disabled_sources).
        """
        disabled = self.state.disabled_sources()
        good = [c for c in cands if c.score >= self.s.min_score]
        shown = [c for c in good if source_key(c.source) not in disabled]
        return shown[: self.s.max_candidates], len(good) - len(shown)

    async def search_all(self, item_id: str, query: str | None = None,
                         use_cache: bool = False) -> SearchResult:
        """Every EPUB release Shelfmark finds, scored and sorted best-first (no filtering),
        plus any per-source failures such as a rate-limited Anna's Archive.

        Every live search is saved; ``use_cache`` returns a saved result for the same query if it is
        younger than SEARCH_CACHE_HOURS (that's how pre-searched books open instantly).
        """
        row = self.state.get(item_id)
        if not row:
            raise KeyError(item_id)
        q = (query or "").strip()
        if q and q != self.default_query(row):
            self.state.update(item_id, query=q)
        q = q or self.default_query(row)
        if use_cache:
            hit = self.state.load_search(item_id, q, self.s.search_cache_seconds)
            if hit:
                return SearchResult.from_data(*hit)
        # Only override Shelfmark's own title/author query building when the user edited the query.
        manual = q if q != build_query(row["clean_title"], row["author"]) else ""

        def rank(t: str, a: str) -> float:
            return score(t, a, row["clean_title"], row["title"], row["author"])

        res = await self.sm.search(row["clean_title"], primary_author(row["author"]),
                                   manual_query=manual, book_id=item_id, rank=rank)
        for c in res.cands:
            c.score = rank(c.title, c.author)
        res.cands.sort(key=lambda c: (c.score, c.popularity), reverse=True)
        res.searched_at = time.time()
        self.state.save_search(item_id, q, res.to_data(), complete=not res.warnings,
                               max_age=self.s.search_cache_seconds)
        return res

    # ---- skipping ----------------------------------------------------------------
    def skip(self, item_id: str) -> None:
        self.state.update(item_id, status="skipped", message="")

    def unskip(self, item_id: str) -> None:
        self.state.update(item_id, status="missing", message="")

    def give_up(self, item_id: str) -> None:
        """Tried everything: park it for good (unlike "skipped", which means "later")."""
        self.state.update(item_id, status="given_up", message="")

    # Bulk actions on the Books page: action -> (statuses it applies to, new status).
    # Queued/downloading/done books are never touched.
    BULK_ACTIONS = {
        "skip": (("missing", "failed", "given_up"), "skipped"),
        "giveup": (("missing", "failed", "skipped"), "given_up"),
        "restore": (("skipped", "given_up"), "missing"),
    }

    def bulk_set(self, item_ids: list[str], action: str) -> tuple[int, int]:
        """Apply a bulk action. Returns (changed, left alone because their status doesn't allow it)."""
        if action not in self.BULK_ACTIONS:
            raise ValueError(f"Unknown bulk action {action!r}")
        allowed, new_status = self.BULK_ACTIONS[action]
        changed = ignored = 0
        for item_id in dict.fromkeys(item_ids):
            row = self.state.get(item_id)
            if not row:
                continue
            if row["status"] in allowed:
                self.state.update(item_id, status=new_status, message="")
                changed += 1
            elif row["status"] != new_status:
                ignored += 1
        return changed, ignored

    def unmatch(self, item_id: str) -> None:
        """Forget the chosen release (e.g. after a failed download) and put the book back as missing."""
        row = self.state.get(item_id)
        if not row:
            raise KeyError(item_id)
        if row["status"] in ("queued", "downloading", "done"):
            raise ValueError(f"Can't unmatch a book that is {row['status']}")
        self.state.update(item_id, status="missing", release=None, progress=None, message="")

    # ---- pause processing (e.g. while Shelfmark restarts) -------------------------
    def paused_until(self) -> float:
        return self._paused_until

    def paused(self) -> bool:
        return time.time() < self._paused_until

    def pause_left(self) -> float:
        return max(0.0, self._paused_until - time.time())

    def pause_left_text(self) -> str:
        secs = self.pause_left()
        return f"{int(secs)}s" if secs < 60 else f"{secs / 60:.0f} min"

    def pause(self, minutes: float) -> float:
        self._paused_until = time.time() + max(0.0, minutes) * 60
        self.state.set_pref("paused_until", self._paused_until)
        return self._paused_until

    def resume(self) -> None:
        self._paused_until = 0.0
        self.state.set_pref("paused_until", 0)

    async def _wait_while_paused(self, item_id: str) -> None:
        while self.paused():
            self.state.update(item_id, message=f"Paused, resumes in {self.pause_left_text()}")
            await self._sleep(min(5.0, max(0.05, self.pause_left())))

    # ---- download + place --------------------------------------------------------
    def enqueue(self, item_id: str, release: dict[str, Any]) -> None:
        self.state.update(item_id, status="queued", release=release, progress=None, message="Queued",
                          queued_at=time.time())

    async def _download(self, item_id: str, release: dict[str, Any],
                        on_progress: ProgressCb | None) -> tuple[str, dict[str, Any]]:
        """Queue in Shelfmark and wait; re-queue after a rate-limit cooldown (RATE_LIMIT_RETRIES)."""

        async def progress(state: str, pct: float | None, msg: str) -> None:
            if state == "paused":
                self.state.update(item_id, message=f"Paused, resumes in {self.pause_left_text()}")
            elif state == "unreachable":
                self.state.update(item_id, message="Shelfmark unreachable, retrying")
            elif state in QUEUED_STATES:
                # Parked behind other downloads in Shelfmark's own queue: still "queued" to us too.
                self.state.update(item_id, status="queued", progress=None,
                                  message="Waiting in Shelfmark's queue")
            else:
                label = {"resolving": "Resolving", "locating": "Finding a mirror",
                         "downloading": "Downloading"}.get(state, state.capitalize())
                pct_txt = f" {pct:.0f}%" if isinstance(pct, (int, float)) else ""
                self.state.update(item_id, status="downloading", progress=pct,
                                  message=f"{label}{pct_txt}" + (f": {msg}" if msg else ""))
            if on_progress:
                on_progress(state, pct, msg)

        attempt = lost = 0
        unreachable_since: float | None = None
        sending = "Sending to Shelfmark"
        while True:
            try:
                await self._wait_while_paused(item_id)
                self.state.update(item_id, status="downloading", progress=None, message=sending)
                task_id = await self.sm.queue_download(release)
                unreachable_since = None
                task = await self.sm.wait_for(
                    task_id, self.s.download_timeout, self.s.poll_interval, progress,
                    queue_timeout=self.s.queue_wait_timeout, hold=self.paused, grace=self.s.shelfmark_grace)
                return task_id, task
            except TaskLostError:
                # Shelfmark restarted and forgot it (its queue lives in memory): send it again.
                lost += 1
                if lost > self.s.lost_task_resends:
                    raise
                sending = "Re-sent to Shelfmark after it lost the download"
            except ShelfmarkUnreachable:
                # Couldn't even hand it over (Shelfmark down, no pause set): keep trying for the grace period.
                now = time.monotonic()
                unreachable_since = unreachable_since or now
                if now - unreachable_since > self.s.shelfmark_grace:
                    raise
                self.state.update(item_id, status="queued", progress=None,
                                  message="Shelfmark unreachable, retrying")
                await self._sleep(min(10.0, max(1.0, self.s.poll_interval)))
            except ShelfmarkError as e:
                wait = rate_limit_wait([str(e)])
                if wait is None or attempt >= self.s.rate_limit_retries:
                    raise
                attempt += 1
                self.state.update(item_id, status="queued", progress=None,
                                  message=f"Rate-limited, retrying in ~{int(wait + 5)}s "
                                          f"(attempt {attempt + 1} of {self.s.rate_limit_retries + 1})")
                await self._sleep(wait + 5)

    async def process(self, item_id: str, on_progress: ProgressCb | None = None) -> Path:
        """Download the chosen release for an item and place it next to the audio files."""
        row = self.state.get(item_id)
        if not row or not row.get("release"):
            raise ValueError(f"No release chosen for {item_id}")
        release = row["release"]

        temp: Path | None = None
        try:
            # Validate destination before spending a download on it.
            folder = placer.map_path(row["path"], self.s.path_mappings)
            filename = placer.safe_filename(row["clean_title"], row["author"], "epub")
            final, temp = placer.prepare_target(folder, filename)

            task_id, task = await self._download(item_id, release, on_progress)

            self.state.update(item_id, message="Copying into audiobook folder")
            src = task.get("download_path")
            if self.s.shelfmark_books_dir and src:
                placer.copy_into(placer.map_path(src, self.s.path_mappings), temp)
            else:
                await self.sm.fetch_file(task_id, temp)
            placed = placer.commit(temp, final)

            self.state.update(item_id, message="Asking Audiobookshelf to rescan")
            note = ""
            try:
                await self.abs.scan_item(item_id)
                if not has_ebook(await self.abs.get_item(item_id)):
                    note = " (ABS has not picked up the ebook yet; it may need a library scan)"
            except Exception as e:  # placement already succeeded; don't fail the item
                note = f" (ABS rescan failed: {e})"
            self.state.update(item_id, status="done", progress=100, ebook_path=str(placed),
                              message=f"Saved {placed.name}{note}")
            if is_aa_release(release) and self.aa.configured:
                # Stop counting it as pending (Anna's Archive's own list decides from here), then
                # update the count.
                self.aa.release(aa_md5(release))
                try:
                    await self.aa.refresh(downloaded=aa_md5(release))
                except Exception as e:
                    log.info("Anna's Archive quota check failed: %s", e)
            return placed
        except asyncio.CancelledError:  # cancelled from the Activity page: the caller sets the status
            if temp is not None:
                temp.unlink(missing_ok=True)
            if is_aa_release(release):
                self.aa.release(aa_md5(release))
            raise
        except Exception as e:
            log.exception("Processing %s failed", item_id)
            if temp is not None:
                temp.unlink(missing_ok=True)
            if is_aa_release(release):
                self.aa.release(aa_md5(release))  # a failed download doesn't keep a slot reserved
            self.state.update(item_id, status="failed", message=str(e)[:500])
            raise

    # ---- cancel ------------------------------------------------------------------
    async def cancel_in_shelfmark(self, item_id: str) -> None:
        """Ask Shelfmark to drop this book's download, unless another queued/downloading book is
        sharing the same release (cancelling would pull it out from under that one too)."""
        row = self.state.get(item_id)
        task_id = str(((row or {}).get("release") or {}).get("source_id") or "")
        if not task_id:
            return
        for other in self.state.active():
            if other["item_id"] != item_id and str((other.get("release") or {}).get("source_id")) == task_id:
                return
        try:
            await self.sm.cancel_download(task_id)
        except Exception as e:  # already finished/forgotten in Shelfmark - nothing to cancel
            log.info("Shelfmark cancel for %s: %s", task_id, e)

    def mark_cancelled(self, item_id: str) -> None:
        self.state.update(item_id, status="skipped", release=None, progress=None,
                          message="Cancelled from the queue")

    # ---- bulk queue: pre-searched perfect matches --------------------------------
    def perfect_matches(self, limit: int | None = None, library_id: str | None = None,
                        q: str | None = None) -> list[tuple[str, Candidate]]:
        """Missing books (list order, optionally narrowed like the Books page) whose saved search has a
        release scored 100 from an enabled source: [(item_id, best such candidate)], at most ``limit``.
        Only "missing" books: skipped/given-up ones are never queued automatically."""
        out: list[tuple[str, Candidate]] = []
        for row in self.state.list(status="missing", library_id=library_id or None, q=q or None, limit=100_000):
            hit = self.state.load_search(row["item_id"], self.default_query(row), self.s.search_cache_seconds)
            if not hit:
                continue
            shown, _ = self.select(SearchResult.from_data(*hit).cands)  # enabled sources, best first
            if shown and shown[0].score >= 100:
                out.append((row["item_id"], shown[0]))
                if limit is not None and len(out) >= limit:
                    break
        return out

    def queue_perfect(self, count: int, library_id: str | None = None, q: str | None = None) -> list[str]:
        """Enqueue the next ``count`` pre-searched books that have a 100-score release."""
        picked = self.perfect_matches(max(0, count), library_id, q)
        for item_id, cand in picked:
            self.enqueue(item_id, cand.raw)
        return [item_id for item_id, _ in picked]

    # ---- probing -----------------------------------------------------------------
    async def probe(self, title: str = "Dune", author: str = "Frank Herbert") -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, coro in (
            ("abs_me", self.abs.ping()),
            ("abs_libraries", self.abs.libraries()),
            ("shelfmark_config", self.sm.config()),
            ("shelfmark_status", self.sm.status()),
            ("shelfmark_manual_releases", self.sm.manual_releases(title, author)),
            ("shelfmark_metadata", self.sm.metadata_search(f"{title} {author}")),
            *((("annas_archive_quota", self.aa.refresh()),) if self.aa.configured else ()),
        ):
            t0 = time.monotonic()
            try:
                out[name] = {"ok": True, "data": await coro}
            except Exception as e:
                out[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            out[name]["seconds"] = round(time.monotonic() - t0, 1)
        rels = (out["shelfmark_manual_releases"].get("data") or [])
        out["summary"] = {
            "ok": True,
            "data": {
                "search_mode": ((out["shelfmark_config"].get("data") or {}).get("search_mode")),
                "manual_search_seconds": out["shelfmark_manual_releases"]["seconds"],
                "manual_release_count": len(rels),
                "epub_release_count": sum(1 for r in rels if release_format(r) == "epub"),
                "release_keys": sorted(rels[0].keys()) if rels else [],
                "first_releases": [
                    {k: r.get(k) for k in ("source", "source_id", "title", "format", "size", "language")}
                    for r in rels[:5]
                ],
            },
        }
        return out
