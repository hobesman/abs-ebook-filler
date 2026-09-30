"""Parallel downloads: per-source limits, Shelfmark queue vs download timeouts, shared releases,
rate-limit retries and the shared status poll."""

import asyncio
from collections import Counter

import httpx
import pytest
import respx

from abs_ebook_filler.core.models import Book
from abs_ebook_filler.core.shelfmark_client import ShelfmarkClient, ShelfmarkError
from abs_ebook_filler.web.worker import Worker

RATE = ("direct_download: Unable to reach download source. annas-archive.gl is rate-limited (429); "
        "skipping bypass for ~87s until the cooldown clears.")


def add_books(service, library, names):
    books = []
    for n in names:
        (library / "Author" / n).mkdir(parents=True, exist_ok=True)
        (library / "Author" / n / "01.m4b").write_bytes(b"audio")
        books.append(Book(item_id=n, library_id="lib1", title=n, clean_title=n, author="Author",
                          path=f"/audiobooks/Author/{n}"))
    service.state.sync_missing(books)


def rel(source, sid):
    return {"source": source, "source_id": sid, "title": "T", "format": "epub"}


async def test_worker_per_source_limits(service, library):
    add_books(service, library, ["b1", "b2", "b3", "b4"])
    for n, src in (("b1", "direct_download"), ("b2", "direct_download"), ("b3", "prowlarr"), ("b4", "prowlarr")):
        service.enqueue(n, rel(src, n))

    active, peak, started = Counter(), Counter(), []

    async def fake_process(item_id, on_progress=None):
        src = service.state.get(item_id)["release"]["source"]
        for k in (src, "all"):
            active[k] += 1
            peak[k] = max(peak[k], active[k])
        started.append(item_id)
        await asyncio.sleep(0.05)
        for k in (src, "all"):
            active[k] -= 1

    service.process = fake_process
    worker = Worker(service)  # DOWNLOAD_CONCURRENCY=3, DIRECT_DOWNLOAD_CONCURRENCY=1
    for n in ("b1", "b2", "b3", "b4"):
        worker.submit(n)
    await worker.start()
    await worker.drain()
    await worker.stop()

    assert peak["direct_download"] == 1 and peak["all"] == 3
    assert started[:3] == ["b1", "b3", "b4"]  # b2 passed over while Anna's Archive is busy
    assert sorted(started) == ["b1", "b2", "b3", "b4"]


@respx.mock
async def test_time_in_shelfmark_queue_does_not_count_as_download_time():
    calls = {"n": 0}

    def status(request):
        calls["n"] += 1
        if calls["n"] <= 6:  # ~0.3s parked in Shelfmark's queue, longer than the 0.2s download timeout
            return httpx.Response(200, json={"queued": {"t1": {"status": "queued"}}})
        if calls["n"] == 7:
            return httpx.Response(200, json={"downloading": {"t1": {"status": "downloading", "progress": 50}}})
        return httpx.Response(200, json={"complete": {"t1": {"status": "complete"}}})

    respx.get("http://sm/api/status").mock(side_effect=status)
    async with ShelfmarkClient("http://sm", "key") as c:
        task = await c.wait_for("t1", timeout=0.2, interval=0.05, queue_timeout=5)
    assert task["status"] == "complete"


@respx.mock
async def test_queue_wait_timeout():
    respx.get("http://sm/api/status").mock(return_value=httpx.Response(
        200, json={"queued": {"t1": {"status": "queued"}}}))
    async with ShelfmarkClient("http://sm", "key") as c:
        with pytest.raises(ShelfmarkError, match="Still waiting in Shelfmark's queue"):
            await c.wait_for("t1", timeout=60, interval=0.02, queue_timeout=0.1)


@respx.mock
async def test_status_poll_is_shared_between_downloads():
    calls = {"n": 0}

    def status(request):
        calls["n"] += 1
        state = "downloading" if calls["n"] <= 5 else "complete"
        return httpx.Response(200, json={state: {t: {"status": state} for t in ("a", "b", "c")}})

    route = respx.get("http://sm/api/status").mock(side_effect=status)
    async with ShelfmarkClient("http://sm", "key") as c:
        await asyncio.gather(*(c.wait_for(t, interval=0.05) for t in ("a", "b", "c")))
    assert route.call_count <= 9  # ~6 shared polls; unshared would be ~18


@respx.mock
async def test_two_books_share_one_release(service, library):
    add_books(service, library, ["A", "B"])
    shared = rel("direct_download", "omnibus")
    posts = {"n": 0}

    def post(request):
        posts["n"] += 1
        if posts["n"] == 1:
            return httpx.Response(200, json={"status": "queued"})
        return httpx.Response(500, json={"error": "Release is already in the download queue"})

    respx.post("http://sm/api/releases/download").mock(side_effect=post)
    respx.get("http://sm/api/status").mock(return_value=httpx.Response(
        200, json={"complete": {"omnibus": {"status": "complete"}}}))
    respx.get("http://sm/api/localdownload").mock(return_value=httpx.Response(200, content=b"EPUB"))
    respx.post(url__regex=r"http://abs/api/items/.*/scan").mock(return_value=httpx.Response(200, json={}))
    respx.get(url__regex=r"http://abs/api/items/[^/]+$").mock(return_value=httpx.Response(
        200, json={"media": {"ebookFile": {"x": 1}}}))

    for n in ("A", "B"):
        service.enqueue(n, shared)
    await asyncio.gather(service.process("A"), service.process("B"))
    for n in ("A", "B"):
        assert service.state.get(n)["status"] == "done"
        assert (library / "Author" / n / f"{n} - Author.epub").read_bytes() == b"EPUB"


@respx.mock
async def test_rate_limited_download_is_retried_after_cooldown(service, library):
    add_books(service, library, ["R"])
    respx.post("http://sm/api/releases/download").mock(return_value=httpx.Response(200, json={"status": "queued"}))
    respx.get("http://sm/api/status").mock(side_effect=[
        httpx.Response(200, json={"error": {"R1": {"status": "error", "status_message": RATE}}}),
        httpx.Response(200, json={"complete": {"R1": {"status": "complete"}}}),
    ])
    respx.get("http://sm/api/localdownload").mock(return_value=httpx.Response(200, content=b"EPUB"))
    respx.post("http://abs/api/items/R/scan").mock(return_value=httpx.Response(200, json={}))
    respx.get("http://abs/api/items/R").mock(return_value=httpx.Response(200, json={"media": {"ebookFile": {}}}))

    slept = []

    async def fake_sleep(s):
        slept.append(s)

    service._sleep = fake_sleep
    service.enqueue("R", rel("direct_download", "R1"))
    await service.process("R")
    assert slept == [92]  # 87s cooldown + 5s margin
    assert service.state.get("R")["status"] == "done"


@respx.mock
async def test_rate_limit_retries_are_capped(service, library):
    add_books(service, library, ["X"])
    respx.post("http://sm/api/releases/download").mock(return_value=httpx.Response(200, json={"status": "queued"}))
    respx.get("http://sm/api/status").mock(return_value=httpx.Response(
        200, json={"error": {"X1": {"status": "error", "status_message": RATE}}}))

    async def fake_sleep(s):
        pass

    service._sleep = fake_sleep
    service.enqueue("X", rel("direct_download", "X1"))
    with pytest.raises(ShelfmarkError):
        await service.process("X")
    row = service.state.get("X")
    assert row["status"] == "failed" and "429" in row["message"]
