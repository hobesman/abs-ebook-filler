import httpx
import respx

from abs_ebook_filler.core.abs_client import ABSClient, has_ebook, is_missing_ebook, item_to_book


def _item(id_, audio=True, ebook=None, lib_ebook=False, title="Stormlight Archive 1 - The Way of Kings"):
    return {
        "id": id_,
        "libraryId": "lib1",
        "path": f"/audiobooks/Brandon Sanderson/{id_}",
        "media": {
            "metadata": {"title": title, "authorName": "Brandon Sanderson",
                         "seriesName": "Stormlight Archive #1"},
            "audioFiles": [{"ino": "1"}] if audio else [],
            "ebookFile": ebook,
        },
        "libraryFiles": [{"fileType": "ebook"}] if lib_ebook else [{"fileType": "audio"}],
    }


def test_missing_ebook_rules():
    assert is_missing_ebook(_item("a"))
    assert not is_missing_ebook(_item("b", ebook={"ebookFormat": "epub"}))
    assert not is_missing_ebook(_item("c", lib_ebook=True))  # supplementary ebook
    assert not is_missing_ebook(_item("d", audio=False))  # ebook-only item
    assert has_ebook({"media": {"ebookFormat": "epub"}})  # minified item shape


def test_item_to_book_cleans_title():
    b = item_to_book(_item("a"))
    assert b.title == "Stormlight Archive 1 - The Way of Kings"
    assert b.clean_title == "The Way of Kings"
    assert b.author == "Brandon Sanderson"
    assert b.path == "/audiobooks/Brandon Sanderson/a"


@respx.mock
async def test_missing_ebooks_paginates_and_filters():
    respx.get("http://abs/api/libraries").mock(return_value=httpx.Response(200, json={
        "libraries": [{"id": "lib1", "mediaType": "book"}, {"id": "pod", "mediaType": "podcast"}]}))
    route = respx.get("http://abs/api/libraries/lib1/items")
    route.side_effect = [
        httpx.Response(200, json={"results": [_item("a"), _item("b", ebook={"x": 1})], "total": 3}),
        httpx.Response(200, json={"results": [_item("c")], "total": 3}),
    ]
    async with ABSClient("http://abs", "tok") as c:
        books = await c.missing_ebooks()
    assert [b.item_id for b in books] == ["a", "c"]
    assert route.call_count == 2
    assert route.calls[0].request.headers["Authorization"] == "Bearer tok"


@respx.mock
async def test_overlapping_libraries_are_deduped_by_path():
    respx.get("http://abs/api/libraries").mock(return_value=httpx.Response(200, json={
        "libraries": [{"id": "all", "mediaType": "book"}, {"id": "kids", "mediaType": "book"}]}))
    same = _item("a")
    respx.get("http://abs/api/libraries/all/items").mock(return_value=httpx.Response(
        200, json={"results": [same], "total": 1}))
    respx.get("http://abs/api/libraries/kids/items").mock(return_value=httpx.Response(
        200, json={"results": [{**same, "id": "a-kids", "libraryId": "kids"}], "total": 1}))
    async with ABSClient("http://abs", "tok") as c:
        books = await c.missing_ebooks()
    assert [b.item_id for b in books] == ["a"]
