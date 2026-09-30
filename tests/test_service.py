import httpx
import pytest
import respx

from abs_ebook_filler.web.worker import Worker

ITEM = {
    "id": "li_1",
    "libraryId": "lib1",
    "path": "/audiobooks/Brandon Sanderson/The Way of Kings",
    "media": {"metadata": {"title": "Stormlight Archive 1 - The Way of Kings",
                           "authorName": "Brandon Sanderson"},
              "audioFiles": [{"ino": "1"}], "ebookFile": None},
    "libraryFiles": [],
}
REL = {"source": "direct_download", "source_id": "md5a", "title": "The Way of Kings",
       "format": "epub", "extra": {"author": "Brandon Sanderson"}}


def mock_abs(ebook_after_scan=True):
    respx.get("http://abs/api/libraries").mock(return_value=httpx.Response(
        200, json={"libraries": [{"id": "lib1", "name": "Books", "mediaType": "book"}]}))
    respx.get("http://abs/api/libraries/lib1/items").mock(return_value=httpx.Response(
        200, json={"results": [ITEM], "total": 1}))
    after = {**ITEM, "media": {**ITEM["media"], "ebookFile": {"ebookFormat": "epub"} if ebook_after_scan else None}}
    respx.get("http://abs/api/items/li_1").mock(return_value=httpx.Response(200, json=after))
    respx.post("http://abs/api/items/li_1/scan").mock(return_value=httpx.Response(200, json={"result": "UPDATED"}))


def mock_shelfmark():
    respx.get("http://sm/api/releases").mock(return_value=httpx.Response(200, json={"releases": [REL]}))
    download = respx.post("http://sm/api/releases/download").mock(
        return_value=httpx.Response(200, json={"status": "queued"}))
    respx.get("http://sm/api/status").mock(return_value=httpx.Response(
        200, json={"complete": {"md5a": {"status": "complete", "download_path": "/books/x.epub"}}}))
    respx.get("http://sm/api/localdownload").mock(return_value=httpx.Response(200, content=b"EPUBDATA"))
    return download


@respx.mock
async def test_scan_search_process_end_to_end(service, library):
    mock_abs()
    mock_shelfmark()
    assert (await service.scan())["found"] == 1
    row = service.state.get("li_1")
    assert row["clean_title"] == "The Way of Kings"
    assert service.default_query(row) == "The Way of Kings Brandon Sanderson"

    cands = await service.search("li_1")
    assert cands[0].score >= 95

    service.enqueue("li_1", cands[0].raw)
    placed = await service.process("li_1")
    assert placed == library / "Brandon Sanderson" / "The Way of Kings" / "The Way of Kings - Brandon Sanderson.epub"
    assert placed.read_bytes() == b"EPUBDATA"
    row = service.state.get("li_1")
    assert row["status"] == "done" and "Saved" in row["message"]


@respx.mock
async def test_process_fails_cleanly_when_file_exists(service, library):
    mock_abs()
    download = mock_shelfmark()
    await service.scan()
    existing = library / "Brandon Sanderson" / "The Way of Kings" / "The Way of Kings - Brandon Sanderson.epub"
    existing.write_bytes(b"old")
    service.enqueue("li_1", REL)
    with pytest.raises(Exception):
        await service.process("li_1")
    assert existing.read_bytes() == b"old"
    assert service.state.get("li_1")["status"] == "failed"
    # Destination is validated before anything is sent to Shelfmark.
    assert download.call_count == 0


@respx.mock
async def test_rescan_drops_items_that_got_an_ebook_but_keeps_done(service):
    mock_abs()
    await service.scan()
    service.state.update("li_1", status="skipped")
    respx.get("http://abs/api/libraries/lib1/items").mock(return_value=httpx.Response(
        200, json={"results": [], "total": 0}))
    res = await service.scan()
    assert res["removed"] == 1 and service.state.get("li_1") is None


@respx.mock
async def test_unmatch_after_failure(service):
    mock_abs()
    await service.scan()
    service.state.update("li_1", status="failed", release=REL, message="No mirrors")
    service.unmatch("li_1")
    row = service.state.get("li_1")
    assert row["status"] == "missing" and row["release"] is None and row["message"] == ""

    service.state.update("li_1", status="queued", release=REL)
    with pytest.raises(ValueError):
        service.unmatch("li_1")  # never yank a release out from under the worker


@respx.mock
async def test_disabled_sources_filtered_before_limit(service):
    mock_abs()
    await service.scan()
    mam = [{**REL, "source": "prowlarr", "source_id": f"mam{i}", "indexer": "MyAnonamouse"} for i in range(10)]
    aa = {**REL, "source_id": "aa1", "indexer": "Direct Download"}
    respx.get("http://sm/api/releases").mock(return_value=httpx.Response(200, json={"releases": mam + [aa]}))
    service.s.max_candidates = 8

    all_cands = await service.search_all("li_1")
    shown, hidden = service.select(all_cands)
    assert len(shown) == 8 and hidden == 0

    assert service.state.set_source_enabled("MyAnonamouse", False) == {"myanonamouse"}
    shown, hidden = service.select(all_cands)
    assert [c.raw["source_id"] for c in shown] == ["aa1"] and hidden == 10

    service.state.set_source_enabled("myanonamouse", True)  # case-insensitive
    assert service.state.disabled_sources() == set()


@respx.mock
async def test_worker_resumes_interrupted_items(service):
    mock_abs()
    mock_shelfmark()
    await service.scan()
    service.state.update("li_1", status="downloading", release=REL)  # simulate crash mid-download
    worker = Worker(service)
    await worker.start()
    await worker.queue.join()
    await worker.stop()
    assert service.state.get("li_1")["status"] == "done"
