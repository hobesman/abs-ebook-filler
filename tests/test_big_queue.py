"""Big queues: the scheduler's pass stays cheap, the database is tuned, the Activity queue is paged."""

import time

import httpx
import respx
from fastapi.testclient import TestClient

from abs_ebook_filler.core.annas import AnnasQuota
from abs_ebook_filler.core.models import Book
from abs_ebook_filler.web.app import create_app
from abs_ebook_filler.web.worker import Worker


def add_books(service, names):
    service.state.sync_missing([Book(item_id=n, library_id="l", title=n, clean_title=n, author="A",
                                     path=f"/audiobooks/A/{n}") for n in names])


async def test_claim_pass_over_3000_held_direct_downloads_is_cheap(service):
    # Wait-for-a-fast-slot on, no slots left, 3000 Anna's Archive books ahead of one torrent.
    service.aa = AnnasQuota(service.state, "KEY", "https://aa.test")
    now = time.time()
    service.state.set_pref("aa_quota", {"per_day": 1, "checked_at": now, "recent": {"0" * 32: {"lo": now, "hi": now}},
                                        "pending": {}, "zero_until": 0, "error": ""})
    service.set_aa_wait(True)
    worker = Worker(service)
    for n in range(3000):
        md5 = f"{n + 1:032x}"  # (none of them is the one already counted in the window)
        worker._waiting.append(f"dd{n}")
        worker._source[f"dd{n}"] = "direct_download"
        worker._release[f"dd{n}"] = {"source": "direct_download", "source_id": md5}
    worker._waiting.append("tor")
    worker._source["tor"] = "prowlarr"
    worker._release["tor"] = {"source": "prowlarr", "source_id": "t"}
    worker._claim()  # warm up the in-memory settings

    calls = {"pref": 0, "update": 0}
    real_pref, real_update = service.state.get_pref, service.state.update

    def counting_pref(*a, **k):
        calls["pref"] += 1
        return real_pref(*a, **k)

    def counting_update(*a, **k):
        calls["update"] += 1
        return real_update(*a, **k)

    service.state.get_pref, service.state.update = counting_pref, counting_update
    worker._waiting.append("tor2")
    worker._source["tor2"] = "prowlarr"
    worker._release["tor2"] = {"source": "prowlarr", "source_id": "t2"}
    t0 = time.perf_counter()
    claimed = worker._claim()
    elapsed = time.perf_counter() - t0
    assert claimed == "tor2"            # torrents still get through behind the held books
    assert calls == {"pref": 0, "update": 0}  # no database reads or writes per book
    assert elapsed < 0.5
    assert len(worker.blocked_ids()) == 3000
    await worker.stop()


def test_database_uses_wal_and_single_book_lookup(service):
    assert service.state._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    add_books(service, ["x", "y"])
    service.state.save_search("x", "x A", {"cands": [], "warnings": []}, complete=True)
    assert service.state.has_saved_search("x", 3600) and not service.state.has_saved_search("y", 3600)


def test_move_to_edge_and_worker_move(service):
    add_books(service, ["q1", "q2", "q3"])
    for n in ("q1", "q2", "q3"):
        service.enqueue(n, {"source": "prowlarr", "source_id": n})
    order = lambda: [r["item_id"] for r in service.state.active()]  # noqa: E731
    assert service.state.move_to_edge("q3", front=True)
    assert order() == ["q3", "q1", "q2"]
    assert service.state.move_to_edge("q3", front=False)
    assert order() == ["q1", "q2", "q3"]


async def test_worker_reorder_within_a_page_keeps_other_positions(service):
    add_books(service, ["a", "b", "c", "d", "e"])
    worker = Worker(service, concurrency=1)
    for n in "abcde":
        service.enqueue(n, {"source": "prowlarr", "source_id": n})
        worker.submit(n)
    worker.reorder(["d", "b"])  # swap b and d; a, c, e stay where they are
    assert worker._waiting == ["a", "d", "c", "b", "e"]
    worker.move("e", front=True)
    assert worker._waiting[0] == "e"


@respx.mock
def test_activity_queue_is_paged(settings, service):
    respx.get("http://abs/api/libraries").mock(return_value=httpx.Response(200, json={"libraries": []}))
    names = [f"b{n:03d}" for n in range(250)]
    add_books(service, names)
    service.pause(30)  # keep the worker from starting anything
    for n in names:
        service.enqueue(n, {"source": "prowlarr", "source_id": n})
    app = create_app(settings, service)
    with TestClient(app) as c:
        c.auth = ("admin", "pw")
        page1 = c.get("/activity").text
        assert "In progress <span class=\"count\">(250)</span>" in page1
        assert "Page 1 of 3" in page1 and page1.count('class=" movable"') == 100
        page3 = c.get("/activity/rows", params={"page": 3}).text
        assert "Page 3 of 3" in page3 and page3.count('class=" movable"') == 50
        assert '"page": 3' in page3  # the auto-refresh stays on page 3

        moved = c.post(f"/queue/{names[0]}/last", data={"page": "3"}).text
        assert "Page 3 of 3" in moved
        assert service.state.active()[-1]["item_id"] == names[0]
        assert app.state.worker._waiting[-1] == names[0]
        c.post(f"/queue/{names[-1]}/next", data={"page": "1"})
        assert service.state.active()[0]["item_id"] == names[-1]
