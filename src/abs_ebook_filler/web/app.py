"""FastAPI + HTMX web UI."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..core.config import Settings, get_settings
from ..core.models import SearchResult
from ..core.presearch import PreSearcher, is_rate_limit
from ..core.service import Service
from ..core.shelfmark_client import ShelfmarkError
from ..core.state import STATUSES, source_key
from .worker import Worker

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
security = HTTPBasic(realm="abs-ebook-filler")
SEARCH_CACHE_SECONDS = 15 * 60  # in-memory task reuse; saved results in SQLite last SEARCH_CACHE_HOURS


def _ago(ts: float | None) -> str:
    """'3 min' / '2 h' for results older than a minute, else ''."""
    if not ts:
        return ""
    secs = time.time() - ts
    if secs < 60:
        return ""
    if secs < 3600:
        return f"{int(secs // 60)} min"
    return f"{secs / 3600:.0f} h"


def create_app(settings: Settings | None = None, service: Service | None = None) -> FastAPI:
    settings = settings or get_settings()
    if not settings.web_password:
        raise RuntimeError("WEB_PASSWORD must be set to run the web UI")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = service or Service(settings)
        worker = Worker(svc, settings.worker_concurrency)
        app.state.svc = svc
        app.state.worker = worker
        app.state.cands = {}  # item_id -> list[Candidate] from the last search
        app.state.searches = {}  # item_id -> (query, started_at, asyncio.Task) for prefetch/reuse
        # Interactive searches (panel loads, rapid prefetch) are the tasks in app.state.searches;
        # pre-search waits while any of them is running.
        app.state.presearch = PreSearcher(
            svc, is_busy=lambda: any(not t.done() for _, _, t in app.state.searches.values()))
        app.state.libraries = {}
        try:
            app.state.libraries = {l["id"]: l.get("name", l["id"]) for l in await svc.abs.libraries()}
        except Exception as e:
            log.warning("Could not load ABS libraries at startup: %s", e)
        await worker.start()
        try:
            yield
        finally:
            app.state.presearch.stop()
            await app.state.presearch.wait()
            for _, _, task in app.state.searches.values():
                task.cancel()
            await worker.stop()
            if service is None:
                await svc.aclose()

    app = FastAPI(title="abs-ebook-filler", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def auth(creds: HTTPBasicCredentials = Depends(security)) -> str:
        ok_user = secrets.compare_digest(creds.username.encode(), settings.web_user.encode())
        ok_pass = secrets.compare_digest(creds.password.encode(), settings.web_password.encode())
        if not (ok_user and ok_pass):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unauthorized",
                                headers={"WWW-Authenticate": 'Basic realm="abs-ebook-filler"'})
        return creds.username

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}

    r = APIRouter(dependencies=[Depends(auth)])

    def svc(request: Request) -> Service:
        return request.app.state.svc

    def ctx(request: Request, **kw):
        return {"request": request, "libraries": request.app.state.libraries, **kw}

    def _ready(s: Service) -> set[str]:
        """Books with saved search results, marked ⚡ in the list."""
        return s.state.searched_ids(s.s.search_cache_seconds)

    # ---- missing books -----------------------------------------------------------
    @r.get("/", response_class=HTMLResponse)
    async def index(request: Request, status: str = "missing", library: str = "", q: str = "",
                    flash: str = "", open: str = ""):
        s = svc(request)
        rows = s.state.list(status=status or None, library_id=library or None, q=q or None)
        # `open` = item to show in the panel on load (used by "Search again").
        open_id = open if open and s.state.get(open) else ""
        return templates.TemplateResponse(request, "index.html", ctx(
            request, rows=rows, counts=s.state.counts(), statuses=STATUSES,
            f_status=status, f_library=library, f_q=q, flash=flash, open_id=open_id,
            ready=_ready(s), **_presearch_ctx(request)))

    # ---- pre-search --------------------------------------------------------------
    def _presearch_ctx(request: Request) -> dict:
        s = svc(request)
        ready = s.state.searched_ids(s.s.search_cache_seconds, complete_only=True)
        missing = {r["item_id"] for r in s.state.list(status="missing")}
        return {"pre": request.app.state.presearch.status.to_dict(),
                "pre_ready": len(ready & missing), "pre_missing": len(missing),
                "pre_default": s.s.presearch_count, "cache_hours": s.s.search_cache_hours}

    def _presearch_fragment(request: Request):
        return templates.TemplateResponse(request, "_presearch.html", ctx(request, **_presearch_ctx(request)))

    @r.post("/presearch", response_class=HTMLResponse)
    async def presearch_start(request: Request, count: int = Form(100)):
        request.app.state.presearch.start(max(1, min(count, 2000)))
        return _presearch_fragment(request)

    @r.post("/presearch/stop", response_class=HTMLResponse)
    async def presearch_stop(request: Request):
        pre = request.app.state.presearch
        pre.stop()
        await pre.wait()
        return _presearch_fragment(request)

    @r.get("/presearch/status", response_class=HTMLResponse)
    async def presearch_status(request: Request):
        return _presearch_fragment(request)

    @r.post("/scan")
    async def scan(request: Request):
        try:
            result = await svc(request).scan()
            msg = f"Scan complete: {result['found']} audiobooks without an ebook."
        except Exception as e:
            msg = f"Scan failed: {e}"
        resp = Response(status_code=204)
        resp.headers["HX-Redirect"] = "/?" + urlencode({"flash": msg})
        return resp

    @r.get("/cover/{item_id}")
    async def cover(request: Request, item_id: str, width: int = 120):
        # width=0 -> original size (used by the click-to-zoom lightbox)
        got = await svc(request).abs.cover(item_id, width or None)
        if not got:
            return Response(status_code=404)
        data, ctype = got
        return Response(data, media_type=ctype, headers={"Cache-Control": "max-age=86400"})

    # ---- book panel --------------------------------------------------------------
    def _panel(request: Request, item_id: str, note: str = ""):
        s = svc(request)
        row = s.state.get(item_id)
        if not row:
            raise HTTPException(404, "Unknown item")
        return templates.TemplateResponse(request, "_book_panel.html", ctx(
            request, row=row, query=s.default_query(row), note=note))

    @r.get("/book/{item_id}", response_class=HTMLResponse)
    async def book(request: Request, item_id: str):
        return _panel(request, item_id)

    def _search_task(request: Request, item_id: str, q: str, fresh: bool = False) -> asyncio.Task:
        """Reuse an in-flight or recent search for the same item+query (rapid-mode prefetch).

        Keyed by the *effective* query, so "" (use the default), the default typed into the box,
        and a previously edited query that has since become the default all share one search.
        ``fresh`` (an explicit Search click) ignores a finished result, but still joins one that
        is in flight rather than firing a second, parallel search at Shelfmark.
        """
        s = svc(request)
        row = s.state.get(item_id)
        q = q.strip() or (s.default_query(row) if row else "")
        searches = request.app.state.searches
        now = time.monotonic()
        for key in [k for k, (_, t0, _) in searches.items() if now - t0 > SEARCH_CACHE_SECONDS]:
            searches.pop(key)
        hit = searches.get(item_id)
        if hit and hit[0] == q:
            t = hit[2]
            if not t.done():
                return t
            if not fresh and not t.cancelled() and t.exception() is None:
                return t
        # Cache the unfiltered list so source toggles can re-filter without searching again.
        # Unless this is an explicit Search click, a saved (e.g. pre-searched) result is used as-is.
        task = asyncio.create_task(s.search_all(item_id, q or None, use_cache=not fresh))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())  # silence "never retrieved"
        searches[item_id] = (q, now, task)
        return task

    @r.post("/book/{item_id}/prefetch")
    async def prefetch(request: Request, item_id: str):
        if svc(request).state.get(item_id):
            _search_task(request, item_id, "")
        return Response(status_code=204)

    def _source_chips(s: Service, all_cands: list) -> list[dict]:
        """One toggle per source seen in the results, plus any disabled source (so it can be re-enabled)."""
        disabled = s.state.disabled_sources()
        chips: dict[str, dict] = {}
        for c in all_cands:
            if c.score < s.s.min_score or not c.source:
                continue
            chip = chips.setdefault(source_key(c.source), {"label": c.source, "count": 0})
            chip["count"] += 1
        for key in disabled:
            chips.setdefault(key, {"label": key, "count": 0})
        for key, chip in chips.items():
            chip["enabled"] = key not in disabled
        return sorted(chips.values(), key=lambda ch: (-ch["count"], ch["label"].lower()))

    @r.get("/book/{item_id}/candidates", response_class=HTMLResponse)
    async def candidates(request: Request, item_id: str, q: str = "", fresh: bool = False):
        s = svc(request)
        error = ""
        res = SearchResult()
        try:
            res = await asyncio.shield(_search_task(request, item_id, q, fresh))
        except Exception as e:
            request.app.state.searches.pop(item_id, None)
            error = str(e) if isinstance(e, ShelfmarkError) else f"{type(e).__name__}: {e}"
        cands, hidden = s.select(res.cands)
        request.app.state.cands[item_id] = cands  # download buttons index into this list
        return templates.TemplateResponse(request, "_candidates.html", ctx(
            request, item_id=item_id, cands=cands, error=error, hidden=hidden,
            warnings=res.warnings, rate_limited=any(is_rate_limit(w) for w in [*res.warnings, error]),
            searched_ago=_ago(res.searched_at) if res.from_cache else "",
            sources=_source_chips(s, res.cands)))

    @r.post("/book/{item_id}/sources", response_class=HTMLResponse)
    async def toggle_source(request: Request, item_id: str, source: str = Form(...),
                            enabled: bool = Form(...), q: str = Form("")):
        svc(request).state.set_source_enabled(source, enabled)
        return await candidates(request, item_id, q)

    @r.post("/book/{item_id}/download/{idx}", response_class=HTMLResponse)
    async def download(request: Request, item_id: str, idx: int):
        cands = request.app.state.cands.get(item_id) or []
        if idx < 0 or idx >= len(cands):
            raise HTTPException(400, "Search results expired; search again")
        s = svc(request)
        s.enqueue(item_id, cands[idx].raw)
        request.app.state.worker.submit(item_id)
        resp = _panel(request, item_id, note=f"Queued: {cands[idx].title}")
        resp.headers["HX-Trigger"] = json.dumps({"row-changed": item_id})
        return resp

    @r.post("/book/{item_id}/skip", response_class=HTMLResponse)
    async def skip(request: Request, item_id: str):
        svc(request).skip(item_id)
        resp = _panel(request, item_id, note="Skipped")
        resp.headers["HX-Trigger"] = json.dumps({"row-changed": item_id})
        return resp

    @r.post("/book/{item_id}/unskip", response_class=HTMLResponse)
    async def unskip(request: Request, item_id: str):
        svc(request).unskip(item_id)
        resp = _panel(request, item_id)
        resp.headers["HX-Trigger"] = json.dumps({"row-changed": item_id})
        return resp

    @r.get("/row/{item_id}", response_class=HTMLResponse)
    async def row(request: Request, item_id: str):
        row = svc(request).state.get(item_id)
        if not row:
            return HTMLResponse("")
        return templates.TemplateResponse(request, "_row.html", ctx(request, row=row,
                                                                    ready=_ready(svc(request))))

    # ---- activity ----------------------------------------------------------------
    @r.get("/activity", response_class=HTMLResponse)
    async def activity(request: Request):
        s = svc(request)
        return templates.TemplateResponse(request, "activity.html", ctx(
            request, active=s.state.active(), recent=s.state.recent()))

    @r.get("/activity/rows", response_class=HTMLResponse)
    async def activity_rows(request: Request):
        s = svc(request)
        return templates.TemplateResponse(request, "_activity_rows.html", ctx(
            request, active=s.state.active(), recent=s.state.recent()))

    def _retry(request: Request, item_id: str) -> None:
        s = svc(request)
        row = s.state.get(item_id)
        if not row or not row.get("release"):
            raise HTTPException(400, "Nothing to retry; pick a release first")
        s.enqueue(item_id, row["release"])
        request.app.state.worker.submit(item_id)

    def _unmatch(request: Request, item_id: str) -> None:
        try:
            svc(request).unmatch(item_id)
        except KeyError:
            raise HTTPException(404, "Unknown item")
        except ValueError as e:
            raise HTTPException(409, str(e))

    # Activity-page versions re-render the activity table.
    @r.post("/retry/{item_id}", response_class=HTMLResponse)
    async def retry(request: Request, item_id: str):
        _retry(request, item_id)
        return await activity_rows(request)

    @r.post("/unmatch/{item_id}", response_class=HTMLResponse)
    async def unmatch_activity(request: Request, item_id: str):
        _unmatch(request, item_id)
        return await activity_rows(request)

    # Panel versions re-render the book panel and refresh its list row.
    @r.post("/book/{item_id}/retry", response_class=HTMLResponse)
    async def retry_panel(request: Request, item_id: str):
        _retry(request, item_id)
        resp = _panel(request, item_id, note="Retrying the same release")
        resp.headers["HX-Trigger"] = json.dumps({"row-changed": item_id})
        return resp

    @r.post("/book/{item_id}/unmatch", response_class=HTMLResponse)
    async def unmatch_panel(request: Request, item_id: str):
        _unmatch(request, item_id)
        resp = _panel(request, item_id, note="Unmatched. Pick a different release")
        resp.headers["HX-Trigger"] = json.dumps({"row-changed": item_id})
        return resp

    @r.post("/book/{item_id}/research")
    async def research(request: Request, item_id: str):
        """Unmatch, then open the Missing list with this book's search panel open."""
        _unmatch(request, item_id)
        return RedirectResponse("/?" + urlencode({"status": "missing", "open": item_id}), status_code=303)

    # ---- settings ----------------------------------------------------------------
    @r.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        return templates.TemplateResponse(request, "settings.html", ctx(
            request, cfg=settings.redacted()))

    @r.post("/settings/probe", response_class=HTMLResponse)
    async def probe(request: Request, title: str = Form("Dune"), author: str = Form("Frank Herbert")):
        result = await svc(request).probe(title, author)
        text = json.dumps(result, indent=2, default=str)
        if len(text) > 60000:
            text = text[:60000] + "\n... (truncated; run `cli probe` for the full output)"
        return templates.TemplateResponse(request, "_probe.html", ctx(
            request, result=result, text=text))

    @r.get("/api/state")
    async def api_state(request: Request):
        return JSONResponse(svc(request).state.counts())

    app.include_router(r)
    return app


def run() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = get_settings()
    uvicorn.run(create_app(s), host="0.0.0.0", port=s.web_port, proxy_headers=True)
