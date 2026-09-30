import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from abs_ebook_filler.web.app import create_app

from .test_service import REL, mock_abs, mock_shelfmark


def test_refuses_to_start_without_password(settings):
    with pytest.raises(RuntimeError):
        create_app(settings.model_copy(update={"web_password": ""}))


@respx.mock
def test_auth_and_flow(settings, service):
    mock_abs()
    mock_shelfmark()
    respx.get("http://abs/api/items/li_1/cover").mock(return_value=httpx.Response(200, content=b"img"))
    app = create_app(settings, service)
    with TestClient(app) as c:
        assert c.get("/").status_code == 401
        assert c.get("/", auth=("admin", "wrong")).status_code == 401
        assert c.get("/healthz").status_code == 200

        c.auth = ("admin", "pw")
        r = c.post("/scan")
        assert r.status_code == 204 and "flash=" in r.headers["HX-Redirect"]

        page = c.get("/").text
        assert "The Way of Kings" in page and "Stormlight Archive 1 - The Way of Kings" in page

        assert "Searching Shelfmark" in c.get("/book/li_1").text
        cands = c.get("/book/li_1/candidates").text
        assert "Download" in cands

        r = c.post("/book/li_1/download/0")
        assert r.status_code == 200 and "Queued" in r.text
        assert "row-changed" in r.headers["HX-Trigger"]

        assert c.get("/activity").status_code == 200
        assert c.get("/settings").text.count("********") >= 2
        assert c.get("/cover/li_1").content == b"img"


@respx.mock
def test_failed_book_retry_unmatch_and_search_again(settings, service):
    mock_abs()
    mock_shelfmark()
    app = create_app(settings, service)
    with TestClient(app, auth=("admin", "pw")) as c:
        c.post("/scan")
        service.state.update("li_1", status="failed", release=REL, message="No mirrors")

        activity = c.get("/activity").text
        assert "Search again" in activity and "Unmatch" in activity and "Retry" in activity

        panel = c.get("/book/li_1").text
        assert "Download failed" in panel and "No mirrors" in panel and "Retry download" in panel

        # Panel unmatch -> back to missing, search shown again.
        r = c.post("/book/li_1/unmatch")
        assert r.status_code == 200 and "Searching Shelfmark" in r.text
        assert service.state.get("li_1")["status"] == "missing"

        # Activity "Search again" -> unmatch + land on the list with the panel set to open.
        service.state.update("li_1", status="failed", release=REL, message="boom")
        r = c.post("/book/li_1/research", follow_redirects=False)
        assert r.status_code == 303 and "open=li_1" in r.headers["location"]
        page = c.get(r.headers["location"]).text
        assert 'hx-get="/book/li_1" hx-trigger="load"' in page
        assert service.state.get("li_1")["release"] is None

        # Can't unmatch something that's downloading.
        service.state.update("li_1", status="downloading", release=REL)
        assert c.post("/unmatch/li_1").status_code == 409


@respx.mock
def test_prefetch_is_reused_and_full_size_cover(settings, service):
    mock_abs()
    mock_shelfmark()
    search_route = respx.get("http://sm/api/releases").mock(
        return_value=httpx.Response(200, json={"releases": [REL]}))
    cover = respx.get("http://abs/api/items/li_1/cover").mock(return_value=httpx.Response(200, content=b"big"))
    app = create_app(settings, service)
    with TestClient(app, auth=("admin", "pw")) as c:
        c.post("/scan")
        assert c.post("/book/li_1/prefetch").status_code == 204
        assert "Download" in c.get("/book/li_1/candidates").text
        # Typing the default query into the box is the same search, so it's reused too.
        assert "Download" in c.get("/book/li_1/candidates",
                                   params={"q": "The Way of Kings Brandon Sanderson"}).text
        assert search_route.call_count == 1
        # An edited query runs a new search.
        c.get("/book/li_1/candidates", params={"q": "way of kings"})
        assert search_route.call_count == 2

        # Toggling a source re-filters the cached results without searching again.
        before = search_route.call_count
        # REL has no indexer, so its source label falls back to "direct_download".
        html = c.post("/book/li_1/sources", data={"source": "direct_download", "enabled": "false",
                                                  "q": "way of kings"}).text
        assert "hidden by disabled sources" in html and 'aria-pressed="false"' in html
        assert service.state.disabled_sources() == {"direct_download"}
        html = c.post("/book/li_1/sources", data={"source": "direct_download", "enabled": "true",
                                                  "q": "way of kings"}).text
        assert "Download</button>" in html
        assert search_route.call_count == before

        assert c.get("/cover/li_1", params={"width": 0}).content == b"big"
        assert "width" not in cover.calls[-1].request.url.params
