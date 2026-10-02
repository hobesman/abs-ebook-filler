"""Bulk Skip / Give up / Back to missing, and the "Given up" status."""

import httpx
import respx
from fastapi.testclient import TestClient

from abs_ebook_filler.core.autoretry import AutoRetry
from abs_ebook_filler.core.models import Book
from abs_ebook_filler.core.state import status_label
from abs_ebook_filler.web.app import create_app


def add_books(service, names):
    service.state.sync_missing([Book(item_id=n, library_id="lib1", title=n, clean_title=n, author="Author",
                                     path=f"/audiobooks/Author/{n}") for n in names])


def test_status_label():
    assert status_label("given_up") == "Given up"
    assert status_label("missing") == "Missing"


def test_bulk_set_only_touches_applicable_books(service):
    add_books(service, ["m", "f", "s", "g", "q", "d"])
    service.state.update("f", status="failed")
    service.state.update("s", status="skipped")
    service.state.update("g", status="given_up")
    service.state.update("q", status="queued")
    service.state.update("d", status="done")
    status = lambda n: service.state.get(n)["status"]  # noqa: E731

    assert service.bulk_set(["m", "f", "s", "g", "q", "d"], "giveup") == (3, 2)  # q, d untouched
    assert [status(n) for n in "mfsgqd"] == ["given_up"] * 4 + ["queued", "done"]

    assert service.bulk_set(["m", "f"], "skip") == (2, 0)
    assert service.bulk_set(["m", "s", "q"], "restore") == (2, 1)
    assert status("m") == "missing" and status("s") == "missing" and status("q") == "queued"


def test_given_up_books_are_left_alone(service):
    add_books(service, ["g", "f"])
    service.state.update("g", status="given_up", release={"source": "prowlarr", "source_id": "1"})
    service.state.update("f", status="failed", release={"source": "prowlarr", "source_id": "2"}, message="x")
    submitted = []
    AutoRetry(service, submitted.append).run_once()
    assert submitted == ["f"]  # auto-retry only touches failed books
    assert "g" not in [r["item_id"] for r in service.state.list(status="missing")]
    # ...but a given-up book that ABS no longer lists as missing (an ebook appeared) is dropped.
    service.state.sync_missing([])
    assert service.state.get("g") is None


@respx.mock
def test_bulk_routes_and_given_up_ui(settings, service):
    respx.get("http://abs/api/libraries").mock(return_value=httpx.Response(200, json={"libraries": []}))
    add_books(service, ["b1", "b2", "b3"])
    app = create_app(settings, service)
    with TestClient(app) as c:
        c.auth = ("admin", "pw")
        page = c.get("/").text
        assert 'id="bulk-form"' in page and 'class="row-sel" name="ids" value="b1"' in page
        assert "Given up (0)" in page  # new filter option

        r = c.post("/bulk/giveup", data={"ids": ["b1", "b2"]},
                   headers={"HX-Current-URL": "http://x/?status=missing&q=b"})
        assert r.status_code == 204
        loc = r.headers["HX-Redirect"]
        assert loc.startswith("/?status=missing&q=b&flash=Gave+up+on+2+books")
        assert service.state.get("b1")["status"] == "given_up"

        given = c.get("/", params={"status": "given_up"}).text
        assert "given up" in given and 'data-item-id="b2"' in given
        panel = c.get("/book/b1").text
        assert "Restore" in panel and "Skip for now" in panel and "Give up</button>" not in panel

        c.post("/book/b3/giveup")
        assert service.state.get("b3")["status"] == "given_up"
        c.post("/book/b3/unskip")  # "Restore"
        assert service.state.get("b3")["status"] == "missing"
        assert c.post("/bulk/nonsense", data={"ids": ["b1"]}).status_code == 404
