"""Anna's Archive fast-download quota: tracking, free probes, and holding Direct Downloads."""

import asyncio

import httpx
import respx

from abs_ebook_filler.core.annas import WINDOW, AnnasQuota, is_aa_release
from abs_ebook_filler.core.models import Book
from abs_ebook_filler.web.worker import Worker

AA = "https://aa.test/dyn/api/fast_download.json"
A = "a" * 32
B = "b" * 32
C = "c" * 32


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def info(left, per_day, recent):
    return {"download_url": "https://x", "account_fast_download_info": {
        "downloads_left": left, "downloads_per_day": per_day, "recently_downloaded_md5s": recent}}


def quota(service, clock=None):
    return AnnasQuota(service.state, "KEY", "https://aa.test", clock=clock or Clock())


def aa_rel(md5):
    return {"source": "direct_download", "source_id": md5, "title": "T", "format": "epub",
            "extra": {"direct_download_provider": "annas_archive"}}


def test_is_aa_release():
    assert is_aa_release(aa_rel(A))
    assert is_aa_release({"source": "direct_download", "source_id": A})  # provider missing -> AA
    assert not is_aa_release({"source": "direct_download", "extra": {"direct_download_provider": "libgen"}})
    assert not is_aa_release({"source": "prowlarr", "source_id": "x"})


@respx.mock
async def test_unknown_until_first_download_then_tracked(service):
    clock = Clock()
    q = quota(service, clock)
    route = respx.get(AA).mock(return_value=httpx.Response(200, json=info(24, 25, [A])))
    # Nothing known and nothing safe to probe with: no request is made (it could spend a download).
    await q.refresh()
    assert route.call_count == 0 and q.left() is None and q.slot_available(B)

    # Shelfmark just fast-downloaded A: probing with it is free.
    q.reserve(A)
    await q.refresh(downloaded=A)
    assert route.calls[0].request.url.params["md5"] == A
    assert route.calls[0].request.url.params["key"] == "KEY"
    assert q.left() == 24 and q.status()["per_day"] == 25

    q.reserve(B)  # sent to Shelfmark, not yet confirmed: counted
    assert q.left() == 23
    q.reserve(A)  # already counted: re-downloading is free
    assert q.left() == 23


@respx.mock
async def test_free_probe_uses_md5_safely_inside_window(service):
    clock = Clock()
    q = quota(service, clock)
    route = respx.get(AA).mock(return_value=httpx.Response(200, json=info(24, 25, [A])))
    q.reserve(A)
    await q.refresh(downloaded=A)  # A is now known: downloaded between reserve time and now
    clock.t += WINDOW - 3600       # 17 h later: A is still safely inside the 18-hour window
    await q.refresh()
    assert route.calls[-1].request.url.params["md5"] == A
    clock.t += 3000                # ~17.8 h: too close to leaving the window -> not used
    n = route.call_count
    await q.refresh()
    assert route.call_count == n


@respx.mock
async def test_no_slots_blocks_until_one_frees(service):
    clock = Clock()
    q = quota(service, clock)
    respx.get(AA).mock(return_value=httpx.Response(200, json=info(0, 2, [A, B])))
    q.reserve(A)
    await q.refresh(downloaded=A)
    assert q.left() == 0 and not q.slot_available(C)
    assert q.slot_available(A)  # already downloaded: asking again is free
    assert q.next_free_at() is not None
    clock.t = q.next_free_at() + 1  # by then the oldest has certainly left the window
    assert q.left() >= 1 and q.slot_available(C)


@respx.mock
async def test_probe_with_next_wanted_when_out_of_slots(service):
    q = quota(service)
    respx.get(AA).mock(return_value=httpx.Response(429, json={"download_url": None, "error": "No downloads left"}))
    await q.refresh(next_wanted=C)  # nothing counted by a 429, and now we know there are none left
    assert q.left() == 0 and not q.slot_available(C)
    assert q.status()["error"] == ""


@respx.mock
async def test_probe_with_next_wanted_that_spends_its_slot(service):
    q = quota(service)
    # Numbers are from before C was counted; C isn't in the list yet.
    respx.get(AA).mock(return_value=httpx.Response(200, json=info(1, 2, [A])))
    await q.refresh(next_wanted=C)
    assert q.left() == 0      # A and the just-counted C
    assert q.slot_available(C)  # ...and C's own download is now free


@respx.mock
async def test_bad_key_reported(service):
    q = quota(service)
    q.reserve(A)
    respx.get(AA).mock(return_value=httpx.Response(401, json={"download_url": None, "error": "Invalid secret key"}))
    await q.refresh(downloaded=A)
    assert "rejected the key" in q.status()["error"]


@respx.mock
def test_activity_controls_show_quota_and_toggle(settings, service):
    from fastapi.testclient import TestClient

    from abs_ebook_filler.web.app import create_app

    respx.get("http://abs/api/libraries").mock(return_value=httpx.Response(200, json={"libraries": []}))
    route = respx.get(AA).mock(return_value=httpx.Response(200, json=info(24, 25, [A])))
    service.aa = quota(service)
    service.aa.reserve(A)  # a book already handed to Shelfmark
    app = create_app(settings, service)
    with TestClient(app) as c:
        c.auth = ("admin", "pw")
        ctl = c.post("/aa/check").text  # probes with that book's md5
        assert ">24</strong> / 25 left" in ctl
        assert route.calls[-1].request.url.params["md5"] == A
        ctl = c.post("/aa/wait", data={"enabled": "true"}).text
        assert service.aa_wait_enabled() and "checked" in ctl
        c.post("/aa/wait")
        assert not service.aa_wait_enabled()
        assert "********" in c.get("/settings").text  # key never shown


# ---- worker: Direct Downloads wait for a slot, torrents don't -------------------------
async def test_waiting_direct_download_does_not_block_torrents(settings, service, library):
    for n in ("dd", "tor"):
        (library / "Author" / n).mkdir(parents=True, exist_ok=True)
    service.state.sync_missing([Book(item_id=n, library_id="l", title=n, clean_title=n, author="Author",
                                     path=f"/audiobooks/Author/{n}") for n in ("dd", "tor")])
    service.aa = quota(service)
    service.aa.state.set_pref("aa_quota", {"per_day": 1, "checked_at": service.aa._clock(),
                                           "recent": {A: {"lo": service.aa._clock(), "hi": service.aa._clock()}},
                                           "pending": {}, "zero_until": 0, "error": ""})
    service.set_aa_wait(True)
    service.enqueue("dd", aa_rel(C))
    service.enqueue("tor", {"source": "prowlarr", "source_id": "t1", "title": "T", "format": "epub"})

    started = []

    async def fake_process(item_id, on_progress=None):
        started.append(item_id)

    service.process = fake_process
    worker = Worker(service)
    await worker.start()
    for _ in range(20):
        await asyncio.sleep(0)
    assert started == ["tor"]  # the torrent went ahead; the Direct Download is held
    assert "fast download slot" in service.state.get("dd")["message"]
    assert worker.next_blocked_aa_md5() == C

    service.set_aa_wait(False)  # switching the toggle off releases it
    worker.wake()
    await asyncio.wait_for(worker.drain(), 2)
    await worker.stop()
    assert started == ["tor", "dd"]
    assert C in service.aa._data()["pending"]  # its slot is counted until Anna's Archive confirms it
