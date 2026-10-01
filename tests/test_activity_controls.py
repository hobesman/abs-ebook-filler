"""Auto-retry, queue count, queue reordering and pause/resume."""

import asyncio
import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from abs_ebook_filler.core.autoretry import AutoRetry
from abs_ebook_filler.core.models import Book
from abs_ebook_filler.core.service import Service
from abs_ebook_filler.core.shelfmark_client import ShelfmarkClient, ShelfmarkUnreachable
from abs_ebook_filler.core.state import State
from abs_ebook_filler.web.app import create_app
from abs_ebook_filler.web.worker import Worker


def add_books(service, library, names):
    books = []
    for n in names:
        (library / "Author" / n).mkdir(parents=True, exist_ok=True)
        (library / "Author" / n / "01.m4b").write_bytes(b"audio")
        books.append(Book(item_id=n, library_id="lib1", title=n, clean_title=n, author="Author",
                          path=f"/audiobooks/Author/{n}"))
    service.state.sync_missing(books)


def rel(sid, source="prowlarr"):
    return {"source": source, "source_id": sid, "title": "T", "format": "epub"}


# ---- 1) auto-retry -----------------------------------------------------------------
def test_autoretry_requeues_retryable_failures(service, library):
    add_books(service, library, ["ok", "exists", "norel"])
    service.state.update("ok", status="failed", release=rel("1"), message="No mirrors left")
    service.state.update("exists", status="failed", release=rel("2"),
                         message="Refusing to overwrite existing file: /library/x.epub")
    service.state.update("norel", status="failed", message="boom")
    submitted = []
    now = [1000.0]
    ar = AutoRetry(service, submitted.append, interval_minutes=60, clock=lambda: now[0])

    assert not ar.enabled and not ar.due()
    assert ar.enable() == 1  # runs immediately when switched on
    assert submitted == ["ok"] and service.state.get("ok")["status"] == "queued"
    assert service.state.get("exists")["status"] == "failed"  # a retry can't fix that one
    assert ar.last() == {"at": 1000.0, "count": 1}

    assert not ar.due()
    now[0] += 3600
    assert ar.due()
    service.pause(5)
    assert not ar.due()  # waits while processing is paused
    service.resume()
    ar.disable()
    assert not ar.due() and ar.next_due() is None


async def test_autoretry_loop_runs_when_due(service, library):
    add_books(service, library, ["a"])
    service.state.update("a", status="failed", release=rel("1"), message="x")
    submitted, sleeps = [], []
    ar = AutoRetry(service, submitted.append, interval_minutes=60)
    service.state.set_pref("auto_retry", True)

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    ar._sleep = fake_sleep
    with pytest.raises(asyncio.CancelledError):
        await ar.loop(check_every=60)
    assert submitted == ["a"]  # first check: never run before -> due; second check: not due yet


# ---- 3) reordering -----------------------------------------------------------------
def test_reorder_queue_reassigns_queue_times(service, library):
    add_books(service, library, ["q1", "q2", "q3", "q4"])
    for n in ("q1", "q2", "q3", "q4"):
        service.enqueue(n, rel(n))
    service.state.reorder_queue(["q3", "q1"])  # only these two swap places
    assert [r["item_id"] for r in service.state.active()] == ["q3", "q2", "q1", "q4"]


async def test_worker_reorder_changes_claim_order(service, library):
    add_books(service, library, ["w1", "w2", "w3"])
    worker = Worker(service, concurrency=1)
    for n in ("w1", "w2", "w3"):
        service.enqueue(n, rel(n))
        worker.submit(n)
    worker.reorder(["w3", "w1", "w2"])
    assert worker._claim() == "w3"
    assert worker.claimed_ids() == {"w3"}


# ---- 4) pause / resume -------------------------------------------------------------
async def test_pause_blocks_new_downloads_and_survives_restart(settings, service, library):
    add_books(service, library, ["p1"])
    service.enqueue("p1", rel("p1"))
    worker = Worker(service)
    worker.submit("p1")
    worker.pause(10)
    assert service.paused() and worker._claim() is None
    # A new Service on the same database (container restart) is still paused.
    again = Service(settings, state=State(settings.data_dir + "/state.db"))
    assert again.paused() and 590 < again.pause_left() <= 600
    again.state.close()
    worker.resume()
    assert not service.paused() and worker._claim() == "p1"
    await worker.stop()


@respx.mock
async def test_wait_for_holds_while_paused_without_contacting_shelfmark():
    route = respx.get("http://sm/api/status").mock(return_value=httpx.Response(
        200, json={"complete": {"t1": {"status": "complete"}}}))
    held = {"until": time.monotonic() + 0.15}
    states = []
    async with ShelfmarkClient("http://sm", "key") as c:
        task = await c.wait_for("t1", timeout=0.05, interval=0.02, on_progress=lambda s, p, m: states.append(s),
                                hold=lambda: time.monotonic() < held["until"])
    assert task["status"] == "complete"
    assert states[0] == "paused" and route.call_count == 1  # no polling while held


@respx.mock
async def test_unreachable_shelfmark_tolerated_within_grace():
    calls = {"n": 0}

    def status(request):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"complete": {"t1": {"status": "complete"}}})

    respx.get("http://sm/api/status").mock(side_effect=status)
    async with ShelfmarkClient("http://sm", "key") as c:
        task = await c.wait_for("t1", interval=0.01, grace=5)
    assert task["status"] == "complete"


@respx.mock
async def test_unreachable_shelfmark_fails_after_grace():
    respx.get("http://sm/api/status").mock(side_effect=httpx.ConnectError("refused"))
    async with ShelfmarkClient("http://sm", "key") as c:
        with pytest.raises(ShelfmarkUnreachable, match="gave up"):
            await c.wait_for("t1", interval=0.01, grace=0.05)


@respx.mock
async def test_download_lost_in_shelfmark_restart_is_resent(service, library):
    add_books(service, library, ["L"])
    posts = respx.post("http://sm/api/releases/download").mock(
        return_value=httpx.Response(200, json={"status": "queued"}))
    calls = {"n": 0}

    def status(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"downloading": {"L1": {"status": "downloading"}}})
        if calls["n"] == 2:
            raise httpx.ConnectError("restarting")
        if calls["n"] <= 5:
            return httpx.Response(200, json={})  # Shelfmark is back, but forgot the download
        return httpx.Response(200, json={"complete": {"L1": {"status": "complete"}}})

    respx.get("http://sm/api/status").mock(side_effect=status)
    respx.get("http://sm/api/localdownload").mock(return_value=httpx.Response(200, content=b"EPUB"))
    respx.post("http://abs/api/items/L/scan").mock(return_value=httpx.Response(200, json={}))
    respx.get("http://abs/api/items/L").mock(return_value=httpx.Response(200, json={"media": {"ebookFile": {}}}))

    service.enqueue("L", rel("L1"))
    await service.process("L")
    assert posts.call_count == 2  # sent, lost in the restart, sent again
    assert service.state.get("L")["status"] == "done"


# ---- web: count, controls, reorder routes ------------------------------------------
@respx.mock
def test_activity_page_count_controls_and_reorder(settings, service, library):
    respx.get("http://abs/api/libraries").mock(return_value=httpx.Response(200, json={"libraries": []}))
    add_books(service, library, ["a1", "a2", "a3"])
    service.pause(30)  # keep the worker from picking anything up during the test
    for n in ("a1", "a2", "a3"):
        service.enqueue(n, rel(n))
    app = create_app(settings, service)
    with TestClient(app) as c:
        c.auth = ("admin", "pw")
        # (after startup: the worker resets interrupted "downloading" rows to "queued" when it starts)
        service.state.update("a1", status="downloading")
        page = c.get("/activity").text
        assert "In progress" in page and "(3)" in page and "1 downloading · 2 queued" in page
        assert "Pause processing for" not in page and "Resume now" in page  # paused
        assert "⏸" in page  # badge in the top bar

        html = c.post("/queue/a3/next").text
        assert html.index('data-item-id="a3"') < html.index('data-item-id="a2"')
        c.post("/queue/reorder", data={"ids": ["a2", "a3"]})
        order = [r["item_id"] for r in service.state.active() if r["status"] == "queued"]
        assert order == ["a2", "a3"]

        ctl = c.post("/resume").text
        assert "Pause processing for" in ctl and not service.paused()
        ctl = c.post("/pause", data={"minutes": "5"}).text
        assert "Resume now" in ctl and service.paused()

        service.state.update("a2", status="failed", message="flaky mirror")
        ctl = c.post("/autoretry", data={"enabled": "true"}).text
        assert "checked" in ctl and "re-queued 1" in ctl
        assert service.state.get("a2")["status"] == "queued"
        ctl = c.post("/autoretry").text  # unchecked switch sends nothing
        assert not app.state.autoretry.enabled
