"""Cancelling queued/downloading books, and queueing pre-searched 100-score matches."""

import asyncio

import httpx
import respx
from fastapi.testclient import TestClient

from abs_ebook_filler.core.models import Book, Candidate, SearchResult
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


def save_result(service, item_id, *cands):
    row = service.state.get(item_id)
    res = SearchResult(list(cands))
    service.state.save_search(item_id, service.default_query(row), res.to_data(), complete=True)


def cand(score, source="Direct Download", sid="x"):
    return Candidate(title="T", author="Author", format="epub", size="1MB", language="en", source=source,
                     score=score, raw={"source": "direct_download", "source_id": sid, "title": "T"})


# ---- cancel ------------------------------------------------------------------------
async def test_cancel_waiting_book_is_never_processed(service, library):
    add_books(service, library, ["c1", "c2"])
    processed = []

    async def fake_process(item_id, on_progress=None):
        processed.append(item_id)

    service.process = fake_process
    worker = Worker(service, concurrency=1)
    await worker.start()  # (started first, as in the app; no awaits below until drain)
    for n in ("c1", "c2"):
        service.enqueue(n, rel(n))
        worker.submit(n)
    assert worker.cancel("c2") == "waiting"
    await worker.drain()
    await worker.stop()
    assert processed == ["c1"]


async def test_cancel_running_download_keeps_worker_alive(service, library):
    add_books(service, library, ["slow", "next"])
    started, finished = [], []

    async def fake_process(item_id, on_progress=None):
        started.append(item_id)
        await asyncio.sleep(10 if item_id == "slow" else 0)
        finished.append(item_id)

    service.process = fake_process
    worker = Worker(service, concurrency=1)
    for n in ("slow", "next"):
        service.enqueue(n, rel(n))
        worker.submit(n)
    await worker.start()
    await asyncio.sleep(0.05)
    assert started == ["slow"]
    assert worker.cancel("slow") == "running"
    await asyncio.wait_for(worker.drain(), 2)  # the same worker moves straight on to the next book
    await worker.stop()
    assert finished == ["next"]


@respx.mock
async def test_cancel_in_shelfmark_skips_shared_release(service, library):
    add_books(service, library, ["s1", "s2", "solo"])
    cancel_shared = respx.delete("http://sm/api/download/shared/cancel").mock(return_value=httpx.Response(200))
    cancel_solo = respx.delete("http://sm/api/download/solo1/cancel").mock(return_value=httpx.Response(200))
    service.enqueue("s1", rel("shared"))
    service.enqueue("s2", rel("shared"))
    service.enqueue("solo", rel("solo1"))
    await service.cancel_in_shelfmark("s1")  # s2 still needs that download
    await service.cancel_in_shelfmark("solo")
    assert cancel_shared.call_count == 0 and cancel_solo.call_count == 1
    service.mark_cancelled("solo")
    row = service.state.get("solo")
    assert row["status"] == "skipped" and row["release"] is None


# ---- queue the next N 100-score books ------------------------------------------------
def test_queue_perfect_picks_enabled_100s_in_list_order(service, library):
    add_books(service, library, ["b1", "b2", "b3", "b4", "b5", "b6"])
    save_result(service, "b1", cand(100, sid="b1"))                       # yes
    save_result(service, "b2", cand(95, sid="b2"))                        # not perfect
    save_result(service, "b3", cand(100, source="MyAnonamouse", sid="b3"),
                cand(80, sid="b3x"))                                      # 100 only from a hidden source
    # b4: never searched
    save_result(service, "b5", cand(100, sid="b5"))                       # yes
    save_result(service, "b6", cand(100, sid="b6"))                       # yes, but past the limit
    service.state.set_source_enabled("MyAnonamouse", False)

    assert [i for i, _ in service.perfect_matches()] == ["b1", "b5", "b6"]
    assert service.queue_perfect(2) == ["b1", "b5"]
    assert service.state.get("b1")["status"] == "queued"
    assert service.state.get("b1")["release"]["source_id"] == "b1"
    assert service.state.get("b6")["status"] == "missing"
    assert [i for i, _ in service.perfect_matches()] == ["b6"]  # queued books no longer count


def test_queue_perfect_follows_filter(service, library):
    add_books(service, library, ["Alpha One", "Beta Two"])
    save_result(service, "Alpha One", cand(100, sid="a"))
    save_result(service, "Beta Two", cand(100, sid="b"))
    assert [i for i, _ in service.perfect_matches(q="Beta")] == ["Beta Two"]
    assert service.queue_perfect(5, q="Beta") == ["Beta Two"]
    assert service.state.get("Alpha One")["status"] == "missing"


@respx.mock
def test_presearch_controls_carry_the_current_filter(settings, service, library):
    respx.get("http://abs/api/libraries").mock(return_value=httpx.Response(200, json={"libraries": []}))
    add_books(service, library, ["Alpha One", "Beta Two"])
    app = create_app(settings, service)
    with TestClient(app) as c:
        c.auth = ("admin", "pw")
        page = c.get("/", params={"q": "Beta"}).text
        assert '<input type="hidden" name="q" value="Beta">' in page
        assert "0 of 1 books in this list have results ready" in page
        frag = c.get("/presearch/status", params={"status": "missing", "q": "Beta"}).text
        assert "0 of 1 books in this list" in frag
        r = c.post("/queue-perfect", data={"count": "3", "q": "Beta"},
                   headers={"HX-Current-URL": "http://x/?q=Beta"})
        assert r.headers["HX-Redirect"].startswith("/?q=Beta&flash=")


# ---- web ---------------------------------------------------------------------------
@respx.mock
def test_cancel_and_queue_perfect_routes(settings, service, library):
    respx.get("http://abs/api/libraries").mock(return_value=httpx.Response(200, json={"libraries": []}))
    add_books(service, library, ["q1", "q2", "p1"])
    service.pause(30)  # nothing runs during the test
    for n in ("q1", "q2"):
        service.enqueue(n, rel(n))
    save_result(service, "p1", cand(100, sid="p1"))
    app = create_app(settings, service)
    with TestClient(app) as c:
        c.auth = ("admin", "pw")
        page = c.get("/activity").text
        assert 'hx-post="/queue/q1/cancel"' in page and "cancel-x" in page

        html = c.post("/queue/q1/cancel").text
        assert 'data-item-id="q1"' not in html and 'data-item-id="q2"' in html
        row = service.state.get("q1")
        assert row["status"] == "skipped" and "Cancelled" in row["message"]
        assert c.post("/queue/q1/cancel").status_code == 409  # already skipped

        books = c.get("/").text
        assert "100-score books to the queue" in books and "1 pre-searched book ready" in books
        r = c.post("/queue-perfect", data={"count": "5"})
        assert r.status_code == 204 and "Queued+1+book" in r.headers["HX-Redirect"]
        assert service.state.get("p1")["status"] == "queued"
        assert "p1" in app.state.worker._source  # handed to the (paused) worker
