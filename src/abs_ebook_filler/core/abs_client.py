"""Minimal async Audiobookshelf API client."""

from __future__ import annotations

from typing import Any, AsyncIterator

import httpx

from .matching import clean_title
from .models import Book


def has_audio(item: dict[str, Any]) -> bool:
    media = item.get("media") or {}
    return bool(media.get("audioFiles")) or (media.get("numAudioFiles") or 0) > 0 or (
        media.get("numTracks") or 0
    ) > 0


def has_ebook(item: dict[str, Any]) -> bool:
    media = item.get("media") or {}
    if media.get("ebookFile") or media.get("ebookFormat"):
        return True
    return any((f or {}).get("fileType") == "ebook" for f in item.get("libraryFiles") or [])


def is_missing_ebook(item: dict[str, Any]) -> bool:
    return has_audio(item) and not has_ebook(item)


def item_to_book(item: dict[str, Any]) -> Book:
    meta = (item.get("media") or {}).get("metadata") or {}
    author = meta.get("authorName") or ", ".join(
        a.get("name", "") for a in meta.get("authors") or [] if a.get("name")
    )
    series = meta.get("seriesName") or ", ".join(
        (s.get("name", "") + (f" #{s['sequence']}" if s.get("sequence") else ""))
        for s in meta.get("series") or []
        if s.get("name")
    )
    title = meta.get("title") or ""
    return Book(
        item_id=item["id"],
        library_id=item.get("libraryId", ""),
        title=title,
        clean_title=clean_title(title),
        author=author or "",
        path=item.get("path", ""),
        subtitle=meta.get("subtitle") or "",
        series=series or "",
        isbn=meta.get("isbn") or "",
        asin=meta.get("asin") or "",
    )


class ABSClient:
    def __init__(self, base_url: str, token: str, timeout: float = 60.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "ABSClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def _get(self, path: str, **params) -> Any:
        r = await self._http.get(path, params=params or None)
        r.raise_for_status()
        return r.json()

    async def ping(self) -> dict[str, Any]:
        return await self._get("/api/me")

    async def libraries(self) -> list[dict[str, Any]]:
        data = await self._get("/api/libraries")
        return data.get("libraries", data if isinstance(data, list) else [])

    async def iter_items(self, library_id: str, page_size: int = 100) -> AsyncIterator[dict[str, Any]]:
        page = fetched = 0
        while True:
            data = await self._get(
                f"/api/libraries/{library_id}/items", limit=page_size, page=page, expanded=1
            )
            results = data.get("results") or []
            for item in results:
                yield item
            fetched += len(results)
            page += 1
            if not results or fetched >= (data.get("total") or 0):
                break

    async def missing_ebooks(self, library_ids: list[str] | None = None) -> list[Book]:
        books: list[Book] = []
        seen_paths: set[str] = set()  # libraries can share folders -> same book under two item ids
        for lib in await self.libraries():
            if lib.get("mediaType", "book") != "book":
                continue
            if library_ids and lib["id"] not in library_ids:
                continue
            async for item in self.iter_items(lib["id"]):
                path = item.get("path") or item["id"]
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                if is_missing_ebook(item):
                    item.setdefault("libraryId", lib["id"])
                    books.append(item_to_book(item))
        return books

    async def get_item(self, item_id: str) -> dict[str, Any]:
        return await self._get(f"/api/items/{item_id}", expanded=1)

    async def scan_item(self, item_id: str) -> Any:
        r = await self._http.post(f"/api/items/{item_id}/scan")
        r.raise_for_status()
        return r.json() if r.content else None

    async def cover(self, item_id: str, width: int | None = 120) -> tuple[bytes, str] | None:
        r = await self._http.get(f"/api/items/{item_id}/cover", params={"width": width} if width else None)
        if r.status_code != 200:
            return None
        return r.content, r.headers.get("content-type", "image/jpeg")
