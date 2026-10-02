import httpx
import respx

from abs_ebook_filler.core.presearch import PreSearcher, rate_limit_wait

REL = {"source": "direct_download", "source_id": "md5a", "title": "Book", "format": "epub",
       "extra": {"author": "Author"}}
RATE = ("direct_download: Unable to reach download source. annas-archive.gl is rate-limited (429); "
        "skipping bypass for ~87s until the cooldown clears.")


def _item(n):
    return {"id": f"li_{n}", "libraryId": "lib1", "path": f"/audiobooks/Author/Book {n}",
            "media": {"metadata": {"title": f"Book {n}", "authorName": "Author"},
                      "audioFiles": [{"ino": str(n)}], "ebookFile": None},
            "libraryFiles": []}


def mock_abs(n=3):
    respx.get("http://abs/api/libraries").mock(return_value=httpx.Response(
        200, json={"libraries": [{"id": "lib1", "name": "Books", "mediaType": "book"}]}))
    respx.get("http://abs/api/libraries/lib1/items").mock(return_value=httpx.Response(
        200, json={"results": [_item(i) for i in range(1, n + 1)], "total": n}))


class FakeSleep:
    def __init__(self):
        self.calls = []

    async def __call__(self, secs):
        self.calls.append(secs)


def test_rate_limit_wait_parsing():
    assert rate_limit_wait([RATE]) == 87
    assert rate_limit_wait(["Shelfmark returned 503: rate limit"]) == 90  # no number -> default
    assert rate_limit_wait(["some other error"]) is None
    assert rate_limit_wait([]) is None


@respx.mock
async def test_presearch_searches_in_order_and_saves(service):
    mock_abs(3)
    route = respx.get("http://sm/api/releases").mock(return_value=httpx.Response(200, json={"releases": [REL]}))
    await service.scan()
    sleep = FakeSleep()
    pre = PreSearcher(service, delay=5, sleep=sleep)

    targets = pre.targets(2)
    assert [t["clean_title"] for t in targets] == ["Book 1", "Book 2"]  # list order, limited to N
    st = await pre.run(targets)
    assert st.done == 2 and st.failed == 0 and not st.running
    assert route.call_count == 2
    assert sleep.calls == [5]  # pause between books, not after the last

    # Saved results are served without touching Shelfmark...
    res = await service.search_all("li_1", use_cache=True)
    assert res.from_cache and res.cands[0].raw["source_id"] == "md5a"
    assert route.call_count == 2
    # ...and the next run skips books that are already done.
    assert [t["item_id"] for t in pre.targets(100)] == ["li_3"]


async def test_presearch_targets_follow_the_books_page_filter(service):
    from abs_ebook_filler.core.models import Book

    service.state.sync_missing([
        Book(item_id=i, library_id=lib, title=t, clean_title=t, author=a, path=f"/audiobooks/{i}")
        for i, lib, t, a in [("1", "L1", "Dune", "Frank Herbert"), ("2", "L1", "Emma", "Jane Austen"),
                             ("3", "L2", "Dune Messiah", "Frank Herbert"), ("4", "L1", "Persuasion", "Jane Austen"),
                             ("5", "L1", "Children of Dune", "Frank Herbert")]])
    service.state.update("4", status="skipped")
    service.state.update("5", status="done")
    pre = PreSearcher(service)
    ids = lambda rows: sorted(r["item_id"] for r in rows)  # noqa: E731

    assert ids(pre.targets(100)) == ["1", "2", "3"]                           # default: missing books
    assert ids(pre.targets(100, q="Herbert")) == ["1", "3"]                   # author search
    assert ids(pre.targets(100, q="Herbert", library_id="L1")) == ["1"]       # + library
    assert ids(pre.targets(100, status="skipped")) == ["4"]                   # "come back later" books
    assert ids(pre.targets(100, status=None, q="Dune")) == ["1", "3"]         # All: done book left out
    assert pre.targets(100, status="done") == []


@respx.mock
async def test_presearch_waits_out_rate_limit_and_retries(service):
    mock_abs(1)
    route = respx.get("http://sm/api/releases")
    route.side_effect = [
        httpx.Response(200, json={"releases": [], "errors": [RATE]}),
        httpx.Response(200, json={"releases": [REL]}),
    ]
    respx.get("http://sm/api/metadata/search").mock(return_value=httpx.Response(200, json={"books": []}))
    await service.scan()
    sleep = FakeSleep()
    pre = PreSearcher(service, delay=0, sleep=sleep)
    st = await pre.run(pre.targets(10))
    assert route.call_count == 2 and sleep.calls == [92]  # 87s cooldown + 5s margin
    assert st.done == 1 and st.partial == 0
    assert service.state.searched_ids(3600, complete_only=True) == {"li_1"}


@respx.mock
async def test_presearch_gives_way_to_interactive_searches(service):
    mock_abs(1)
    respx.get("http://sm/api/releases").mock(return_value=httpx.Response(200, json={"releases": [REL]}))
    await service.scan()
    busy = iter([True, True, False])
    sleep = FakeSleep()
    pre = PreSearcher(service, is_busy=lambda: next(busy, False), delay=0, sleep=sleep)
    await pre.run(pre.targets(1))
    assert sleep.calls == [1, 1]  # polled twice while "you" were searching
