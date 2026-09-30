import httpx
import pytest
import respx

from abs_ebook_filler.core.shelfmark_client import ShelfmarkClient, ShelfmarkError, find_task, release_format

REL_EPUB = {"source": "direct_download", "source_id": "md5a", "title": "The Way of Kings",
            "format": "epub", "size": "3 MB", "language": "en", "extra": {"author": "Brandon Sanderson"}}
REL_PDF = {**REL_EPUB, "source_id": "md5b", "format": "pdf"}


def test_find_task_grouped_and_flat():
    grouped = {"queued": {}, "complete": {"md5a": {"id": "md5a", "status": "complete"}}}
    assert find_task(grouped, "md5a")[0] == "complete"
    flat = {"md5a": {"status": "downloading"}}
    assert find_task(flat, "md5a")[0] == "downloading"
    assert find_task(grouped, "zzz") is None


def test_release_format_inferred():
    assert release_format({"format": "EPUB"}) == "epub"
    assert release_format({"title": "Book.epub"}) == "epub"
    assert release_format({"title": "Book"}) == ""
    assert release_format({"format": None, "extra": {"formats": ["mobi", "epub"]}}) == "epub"
    assert release_format({"format": "", "extra": {"formats": ["pdf"]}}) == "pdf"


def test_popularity_from_seeders_or_downloads():
    from abs_ebook_filler.core.shelfmark_client import to_candidate
    assert to_candidate({"seeders": 5337, "format": "epub"}).popularity == 5337
    assert to_candidate({"extra": {"downloads": 192}, "format": "epub"}).popularity == 192
    assert to_candidate({"format": "epub"}).popularity == 0


@respx.mock
async def test_manual_search_params_filter_and_dedupe():
    route = respx.get("http://sm/api/releases").mock(return_value=httpx.Response(
        200, json={"releases": [REL_EPUB, REL_PDF, REL_EPUB]}))
    async with ShelfmarkClient("http://sm", "key") as c:
        res = await c.search("The Way of Kings", "Brandon Sanderson", book_id="li_1")
    cands = res.cands
    assert res.warnings == []
    assert [x.raw["source_id"] for x in cands] == ["md5a"]
    assert cands[0].author == "Brandon Sanderson"
    p = route.calls[0].request.url.params
    assert p["provider"] == "manual" and p["book_id"] == "li_1"
    assert p["title"] == "The Way of Kings" and p["author"] == "Brandon Sanderson"
    assert "manual_query" not in p
    assert route.calls[0].request.headers["X-Api-Key"] == "key"


@respx.mock
async def test_manual_query_passed_when_edited():
    route = respx.get("http://sm/api/releases").mock(return_value=httpx.Response(
        200, json={"releases": [REL_EPUB]}))
    async with ShelfmarkClient("http://sm", "key") as c:
        await c.search("The Way of Kings", "Brandon Sanderson", manual_query="way of kings sanderson")
    assert route.calls[0].request.url.params["manual_query"] == "way of kings sanderson"


@respx.mock
async def test_search_falls_back_to_best_metadata_book():
    def releases(request):
        if request.url.params.get("provider") == "manual":
            return httpx.Response(200, json={"releases": []})
        assert request.url.params["book_id"] == "42"  # best-ranked metadata hit only
        return httpx.Response(200, json={"releases": [REL_EPUB]})

    respx.get("http://sm/api/releases").mock(side_effect=releases)
    respx.get("http://sm/api/metadata/search").mock(return_value=httpx.Response(200, json={"books": [
        {"provider": "hardcover", "provider_id": "99", "title": "Words of Radiance", "authors": ["B S"]},
        {"provider": "hardcover", "provider_id": "42", "title": "The Way of Kings", "authors": ["B S"]},
    ]}))
    rank = lambda t, a: 100 if t == "The Way of Kings" else 0  # noqa: E731
    async with ShelfmarkClient("http://sm", "key") as c:
        res = await c.search("The Way of Kings", "Brandon Sanderson", metadata_hits=1, rank=rank)
    assert len(res.cands) == 1


RATE_LIMIT = ("direct_download: Unable to reach download source. annas-archive.gl is rate-limited (429); "
              "skipping bypass for ~87s until the cooldown clears.")


@respx.mock
async def test_partial_failure_reported_and_no_extra_searches():
    mam = {**REL_EPUB, "source": "prowlarr", "source_id": "mam1", "indexer": "MyAnonamouse"}
    releases = respx.get("http://sm/api/releases").mock(return_value=httpx.Response(
        200, json={"releases": [mam], "errors": [RATE_LIMIT]}))
    metadata = respx.get("http://sm/api/metadata/search").mock(
        return_value=httpx.Response(200, json={"books": []}))
    async with ShelfmarkClient("http://sm", "key") as c:
        res = await c.search("The Way of Kings", "Brandon Sanderson")
    assert [x.source for x in res.cands] == ["MyAnonamouse"]
    assert res.warnings == [RATE_LIMIT]
    assert releases.call_count == 1 and metadata.call_count == 0  # no fallback into the rate limit


@respx.mock
async def test_no_fallback_when_sources_failed_even_with_zero_epubs():
    respx.get("http://sm/api/releases").mock(return_value=httpx.Response(
        200, json={"releases": [REL_PDF], "errors": [RATE_LIMIT]}))
    metadata = respx.get("http://sm/api/metadata/search").mock(
        return_value=httpx.Response(200, json={"books": []}))
    async with ShelfmarkClient("http://sm", "key") as c:
        res = await c.search("X", "Y")
    assert res.cands == [] and res.warnings == [RATE_LIMIT] and metadata.call_count == 0


@respx.mock
async def test_release_searches_run_one_at_a_time():
    import asyncio
    active = peak = 0

    async def slow(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return httpx.Response(200, json={"releases": [REL_EPUB]})

    respx.get("http://sm/api/releases").mock(side_effect=slow)
    async with ShelfmarkClient("http://sm", "key") as c:  # search_concurrency defaults to 1
        await asyncio.gather(c.search("A", "x"), c.search("B", "y"), c.search("C", "z"))
    assert peak == 1


@respx.mock
async def test_search_surfaces_source_error():
    respx.get("http://sm/api/releases").mock(return_value=httpx.Response(
        503, json={"error": "Anna's Archive unreachable"}))
    respx.get("http://sm/api/metadata/search").mock(return_value=httpx.Response(200, json={"books": []}))
    async with ShelfmarkClient("http://sm", "key") as c:
        with pytest.raises(ShelfmarkError, match="unreachable"):
            await c.search("X", "Y")


@respx.mock
async def test_queue_wait_fetch(tmp_path):
    respx.post("http://sm/api/releases/download").mock(return_value=httpx.Response(200, json={"status": "queued"}))
    respx.get("http://sm/api/status").mock(side_effect=[
        httpx.Response(200, json={"downloading": {"md5a": {"status": "downloading", "progress": 50}}}),
        httpx.Response(200, json={"complete": {"md5a": {"status": "complete", "download_path": "/books/x.epub"}}}),
    ])
    respx.get("http://sm/api/localdownload").mock(return_value=httpx.Response(200, content=b"EPUB"))
    seen = []
    async with ShelfmarkClient("http://sm", "key") as c:
        tid = await c.queue_download(REL_EPUB)
        assert tid == "md5a"
        task = await c.wait_for(tid, timeout=5, interval=0, on_progress=lambda s, p, m: seen.append(s))
        assert task["download_path"] == "/books/x.epub"
        await c.fetch_file(tid, tmp_path / "out.epub")
    assert (tmp_path / "out.epub").read_bytes() == b"EPUB"
    assert seen == ["downloading", "complete"]


@respx.mock
async def test_wait_raises_on_error():
    respx.get("http://sm/api/status").mock(return_value=httpx.Response(
        200, json={"error": {"md5a": {"status": "error", "status_message": "No mirrors"}}}))
    async with ShelfmarkClient("http://sm", "key") as c:
        with pytest.raises(ShelfmarkError, match="No mirrors"):
            await c.wait_for("md5a", timeout=5, interval=0)


@respx.mock
async def test_search_timeout_becomes_readable_error():
    respx.get("http://sm/api/releases").mock(side_effect=httpx.ReadTimeout("slow"))
    respx.get("http://sm/api/metadata/search").mock(return_value=httpx.Response(200, json={"books": []}))
    async with ShelfmarkClient("http://sm", "key", search_timeout=7) as c:
        with pytest.raises(ShelfmarkError, match="within 7s"):
            await c.search("X", "Y")


@respx.mock
async def test_bad_key():
    respx.get("http://sm/api/status").mock(return_value=httpx.Response(401))
    async with ShelfmarkClient("http://sm", "bad") as c:
        with pytest.raises(ShelfmarkError, match="API key"):
            await c.status()
