"""Async Shelfmark API client.

Shape notes (from Shelfmark source, confirmed against a live instance with ``cli probe``):
  * ``GET /api/releases`` requires ``provider`` + ``book_id`` (free text alone is a 400).
    ``provider=manual`` searches every enabled source with our ``title``/``author``/``manual_query``.
    Response: ``{"releases": [...], "book": {...}, "sources_searched": [...], "errors"?: [...]}``;
    503 ``{"error": ...}`` when nothing was found and a source failed.
  * Releases serialise as ``asdict(Release)``: source, source_id, title, format,
    language, size, size_bytes, indexer, protocol, content_type, extra{...}.
  * ``POST /api/releases/download`` takes a release dict and returns
    ``{"status": "queued", ...}``; the queue task id is the release's ``source_id``.
  * ``GET /api/status`` is grouped by status: ``{"queued": {id: task}, "complete": {...}, ...}``.
  * ``GET /api/localdownload?id=<task_id>`` streams the finished file.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from .models import Candidate, SearchResult

TERMINAL_OK = {"complete"}
TERMINAL_FAIL = {"error", "cancelled"}
EBOOK_EXTS = ("epub", "mobi", "azw3", "azw", "pdf", "fb2", "cbz", "cbr", "djvu")


class ShelfmarkError(RuntimeError):
    pass


def _first(*vals: Any) -> str:
    for v in vals:
        if v not in (None, "", [], {}):
            if isinstance(v, list):
                return ", ".join(str(x) for x in v)
            return str(v)
    return ""


def release_format(rel: dict[str, Any]) -> str:
    fmt = (rel.get("format") or "").lower().lstrip(".")
    if fmt:
        return fmt
    # Prowlarr releases may leave `format` empty and list what the torrent contains instead.
    formats = [str(f).lower().lstrip(".") for f in (rel.get("extra") or {}).get("formats") or []]
    if "epub" in formats:
        return "epub"
    if formats:
        return formats[0]
    title = (rel.get("title") or "").lower()
    for ext in EBOOK_EXTS:
        if title.endswith("." + ext) or f"[{ext}]" in title or f"({ext})" in title:
            return ext
    return ""


def _popularity(rel: dict[str, Any]) -> int:
    """Seeders for torrents, download count for direct downloads."""
    extra = rel.get("extra") or {}
    for v in (rel.get("seeders"), extra.get("downloads"), extra.get("grabs")):
        try:
            if v not in (None, ""):
                return int(str(v).replace(",", ""))
        except ValueError:
            continue
    return 0


def to_candidate(rel: dict[str, Any], fallback_author: str = "") -> Candidate:
    extra = rel.get("extra") or {}
    return Candidate(
        popularity=_popularity(rel),
        title=_first(rel.get("title"), extra.get("title")),
        author=_first(rel.get("author"), extra.get("author"), extra.get("authors"), fallback_author),
        format=release_format(rel),
        size=_first(rel.get("size"), extra.get("size")),
        language=_first(rel.get("language"), extra.get("language")),
        source=_first(rel.get("source_display_name"), rel.get("indexer"), rel.get("source")),
        raw=rel,
    )


def find_task(status: dict[str, Any], task_id: str) -> tuple[str, dict[str, Any]] | None:
    """Locate a task in the grouped (or flat) /api/status payload."""
    for group, tasks in (status or {}).items():
        if isinstance(tasks, dict):
            if task_id in tasks and isinstance(tasks[task_id], dict):
                return group, tasks[task_id]
            # flat shape: {task_id: {status: ...}}
            if group == task_id and "status" in tasks:
                return str(tasks["status"]), tasks
    return None


class ShelfmarkClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0,
                 transport: httpx.AsyncBaseTransport | None = None, search_timeout: float = 330.0,
                 search_concurrency: int = 1):
        self.search_timeout = search_timeout
        self._search_slots = asyncio.Semaphore(max(1, search_concurrency))
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-Api-Key": api_key},
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "ShelfmarkClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def _get(self, path: str, _timeout: float | None = None, **params) -> Any:
        try:
            r = await self._http.get(
                path,
                params={k: v for k, v in params.items() if v not in (None, "")},
                timeout=_timeout if _timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.TimeoutException:
            secs = _timeout if _timeout is not None else self._http.timeout.read
            raise ShelfmarkError(f"Shelfmark {path} did not answer within {int(secs or 0)}s") from None
        if r.status_code == 401:
            raise ShelfmarkError(f"Shelfmark rejected the API key (401) on {path}")
        if r.status_code >= 400:
            try:
                msg = r.json().get("error") or r.text
            except Exception:
                msg = r.text
            raise ShelfmarkError(f"Shelfmark {path} returned {r.status_code}: {str(msg)[:300]}")
        return r.json()

    # ---- discovery -----------------------------------------------------------------
    async def status(self) -> dict[str, Any]:
        return await self._get("/api/status")

    async def config(self) -> dict[str, Any]:
        return await self._get("/api/config")

    # ---- searching -----------------------------------------------------------------
    @staticmethod
    def _releases_from(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("releases") or data.get("results") or []
        return []

    @staticmethod
    def _errors_from(data: Any) -> list[str]:
        """Per-source failures Shelfmark reports alongside partial results (e.g. a 429 rate limit)."""
        if isinstance(data, dict):
            return [str(e) for e in data.get("errors") or [] if e]
        return []

    async def _release_search(self, **params) -> tuple[list[dict[str, Any]], list[str]]:
        # Release searches hit every source (incl. Anna's Archive, which rate-limits hard), so
        # never run more than `search_concurrency` at once, e.g. a rapid-mode prefetch alongside
        # the search you're looking at.
        async with self._search_slots:
            data = await self._get("/api/releases", _timeout=self.search_timeout, **params)
        return self._releases_from(data), self._errors_from(data)

    async def manual_releases_with_errors(self, title: str, author: str = "", manual_query: str = "",
                                          book_id: str = "abs") -> tuple[list[dict[str, Any]], list[str]]:
        """Release search across all enabled sources using our own title/author.

        ``provider=manual`` makes Shelfmark build the search book from ``title``/``author``
        (no metadata-provider lookup). ``manual_query`` overrides the query text sent to sources.
        """
        return await self._release_search(
            provider="manual", book_id=book_id or "abs", title=title or manual_query,
            author=author, manual_query=manual_query, content_type="ebook",
        )

    async def manual_releases(self, title: str, author: str = "", manual_query: str = "",
                              book_id: str = "abs") -> list[dict[str, Any]]:
        return (await self.manual_releases_with_errors(title, author, manual_query, book_id))[0]

    async def metadata_search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        data = await self._get("/api/metadata/search", query=query, content_type="ebook", limit=limit)
        if isinstance(data, dict):
            return data.get("books") or data.get("results") or []
        return data if isinstance(data, list) else []

    async def releases_for_book(self, provider: str, book_id: str,
                                title: str = "") -> tuple[list[dict[str, Any]], list[str]]:
        return await self._release_search(
            provider=provider, book_id=book_id, title=title, content_type="ebook",
        )

    async def search(self, title: str, author: str, manual_query: str = "", book_id: str = "abs",
                     metadata_hits: int = 1,
                     rank: Callable[[str, str], float] | None = None) -> SearchResult:
        """Manual (title/author) release search; if that finds nothing, try the best metadata book.

        The metadata fallback only runs when the manual search completed cleanly and simply found
        no EPUB. If a source failed (rate limit, timeout...) another full search would just hit the
        same wall, and add to the rate limiting, so the failure is reported instead.
        ``rank(title, author)`` orders metadata hits so the closest book is searched first.
        """
        rels, warnings = await self.manual_releases_with_errors(title, author, manual_query, book_id)
        cands = [to_candidate(r, author) for r in rels]
        if not warnings and not any(c.format == "epub" for c in cands):
            books = await self.metadata_search(manual_query or f"{title} {author}".strip())
            if rank:
                books.sort(key=lambda b: rank(b.get("title") or "", _first(b.get("authors"), b.get("author"))),
                           reverse=True)
            for book in books[:metadata_hits]:
                provider = book.get("provider")
                pid = book.get("provider_id") or book.get("id")
                if not provider or not pid:
                    continue
                book_author = _first(book.get("authors"), book.get("author"), author)
                try:
                    more, errs = await self.releases_for_book(provider, str(pid), book.get("title") or "")
                except ShelfmarkError as e:
                    warnings.append(str(e))
                    continue
                warnings.extend(errs)
                cands.extend(to_candidate(r, book_author) for r in more)
        # Dedupe by (source, source_id) and keep EPUB only.
        seen: set[tuple[str, str]] = set()
        out: list[Candidate] = []
        for c in cands:
            key = (str(c.raw.get("source")), str(c.raw.get("source_id")))
            if key in seen or c.format != "epub":
                continue
            seen.add(key)
            out.append(c)
        return SearchResult(out, list(dict.fromkeys(warnings)))

    # ---- downloading ---------------------------------------------------------------
    async def queue_download(self, release: dict[str, Any]) -> str:
        if not release.get("source_id"):
            raise ShelfmarkError("Release has no source_id; cannot queue it")
        r = await self._http.post("/api/releases/download", json=release)
        if r.status_code >= 400:
            try:
                msg = r.json().get("error") or r.text
            except Exception:
                msg = r.text
            raise ShelfmarkError(f"Shelfmark refused the download ({r.status_code}): {msg}")
        body = r.json() if r.content else {}
        return str(body.get("task_id") or body.get("id") or release["source_id"])

    async def wait_for(
        self,
        task_id: str,
        timeout: float = 600.0,
        interval: float = 3.0,
        on_progress: Callable[[str, float | None, str], Any] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        missing_polls = 0
        while True:
            found = find_task(await self.status(), task_id)
            if found:
                missing_polls = 0
                group, task = found
                state = str(task.get("status") or group).lower()
                if on_progress:
                    res = on_progress(state, task.get("progress"), task.get("status_message") or "")
                    if asyncio.iscoroutine(res):
                        await res
                if state in TERMINAL_OK or group in TERMINAL_OK:
                    return task
                if state in TERMINAL_FAIL or group in TERMINAL_FAIL:
                    raise ShelfmarkError(
                        task.get("status_message") or task.get("last_error_message") or f"Download {state}"
                    )
            else:
                missing_polls += 1
                if missing_polls >= 10:
                    raise ShelfmarkError(f"Task {task_id} disappeared from the Shelfmark queue")
            if time.monotonic() > deadline:
                raise ShelfmarkError(f"Timed out after {int(timeout)}s waiting for task {task_id}")
            await asyncio.sleep(interval)

    async def fetch_file(self, task_id: str, dest: Path) -> None:
        """Stream the finished file to ``dest``."""
        async with self._http.stream("GET", "/api/localdownload", params={"id": task_id}) as r:
            if r.status_code != 200:
                await r.aread()
                raise ShelfmarkError(f"localdownload failed ({r.status_code}): {r.text[:200]}")
            with open(dest, "wb") as fh:
                async for chunk in r.aiter_bytes():
                    fh.write(chunk)
