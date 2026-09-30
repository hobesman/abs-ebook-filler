"""Thin CLI over the shared core: scan, dry-run, probe, run, web."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import typer
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from .core.config import get_settings
from .core.service import Service

app = typer.Typer(add_completion=False, help="Fill Audiobookshelf audiobooks with matching EPUBs via Shelfmark.")
console = Console()


def _service() -> Service:
    return Service(get_settings())


async def _search(svc: Service, item_id: str, query: str | None = None):
    """Search, print any per-source failures (e.g. a rate-limited Anna's Archive), return what to show."""
    res = await svc.search_all(item_id, query, use_cache=True)  # reuse pre-searched results
    for w in res.warnings:
        console.print(f"[yellow]⚠ {w}[/]")
    return svc.select(res.cands)[0]


def _books_table(rows: list[dict]) -> Table:
    t = Table(show_lines=False)
    t.add_column("#", justify="right")
    t.add_column("Clean title")
    t.add_column("ABS title", style="dim")
    t.add_column("Author")
    t.add_column("Status")
    for i, r in enumerate(rows, 1):
        t.add_row(str(i), r["clean_title"], r["title"] if r["title"] != r["clean_title"] else "",
                  r["author"], r["status"])
    return t


def _cands_table(cands) -> Table:
    t = Table()
    for col in ("#", "Score", "Title", "Author", "Size", "Lang", "Source", "Pop."):
        t.add_column(col)
    for i, c in enumerate(cands, 1):
        t.add_row(str(i), str(c.score), c.title, c.author, c.size, c.language, c.source,
                  str(c.popularity or ""))
    return t


@app.command()
def scan(library: Optional[str] = typer.Option(None, help="Only this ABS library id")):
    """Scan ABS and list audiobooks that have no ebook."""
    async def go():
        svc = _service()
        try:
            if library:
                svc.s.abs_library_ids = library
            res = await svc.scan()
            rows = svc.state.list(status="missing")
            console.print(_books_table(rows))
            console.print(f"[bold]{res['found']}[/] audiobooks without an ebook.")
        finally:
            await svc.aclose()
    asyncio.run(go())


@app.command("dry-run")
def dry_run(limit: int = typer.Option(5, help="How many books to search")):
    """Scan, then show the top Shelfmark candidates for the first N missing books. Writes no files."""
    async def go():
        svc = _service()
        try:
            await svc.scan()
            for r in svc.state.list(status="missing")[:limit]:
                console.rule(f"{r['clean_title']} — {r['author']}")
                if r["title"] != r["clean_title"]:
                    console.print(f"[dim]ABS title: {r['title']}[/]")
                console.print(f"[dim]Query: {svc.default_query(r)}[/]")
                try:
                    cands = await _search(svc, r["item_id"])
                except Exception as e:
                    console.print(f"[red]Search failed: {e}[/]")
                    continue
                console.print(_cands_table(cands) if cands else "[yellow]No EPUB releases found[/]")
        finally:
            await svc.aclose()
    asyncio.run(go())


@app.command()
def probe(title: str = typer.Option("Dune", help="Test book title"),
          author: str = typer.Option("Frank Herbert", help="Test book author"),
          out: Optional[str] = typer.Option(None, help="Write raw JSON to this file")):
    """Call both APIs and dump raw responses (use this to confirm Shelfmark's JSON shapes)."""
    async def go():
        svc = _service()
        try:
            result = await svc.probe(title, author)
        finally:
            await svc.aclose()
        text = json.dumps(result, indent=2, default=str)
        if out:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(text)
            console.print(f"Wrote {out}")
        for name, r in result.items():
            mark = "[green]OK[/]" if r["ok"] else f"[red]FAIL[/] {r['error']}"
            secs = f" [dim]({r['seconds']}s)[/]" if "seconds" in r else ""
            console.print(f"{name}: {mark}{secs}")
        console.print_json(json.dumps(result["summary"]["data"], default=str))
        if not out:
            console.print("[dim]Pass --out /data/probe.json to save the full raw responses.[/]")
    asyncio.run(go())


@app.command()
def run(item: Optional[str] = typer.Option(None, help="Only this ABS item id"),
        limit: int = typer.Option(0, help="Stop after N books (0 = all)"),
        retry_skipped: bool = typer.Option(False, help="Also offer books you skipped before")):
    """Interactive terminal flow: pick a release for each missing book."""
    logging.basicConfig(level=logging.WARNING)

    async def go():
        svc = _service()
        try:
            if item:
                await svc.load_item(item)
                rows = [r for r in [svc.state.get(item)] if r]
            else:
                with console.status("Scanning Audiobookshelf…"):
                    await svc.scan()
                rows = svc.state.list(status="missing") + svc.state.list(status="failed")
                if retry_skipped:
                    rows += svc.state.list(status="skipped")
            if limit:
                rows = rows[:limit]
            if not rows:
                console.print("Nothing to do: every audiobook already has an ebook.")
                return
            done = 0
            for n, r in enumerate(rows, 1):
                query = svc.default_query(r)
                while True:
                    console.rule(f"[{n}/{len(rows)}] {r['clean_title']} — {r['author']}")
                    if r["title"] != r["clean_title"]:
                        console.print(f"[dim]ABS title: {r['title']}[/]")
                    with console.status(f"Searching Shelfmark for “{query}”…"):
                        try:
                            cands = await _search(svc, r["item_id"], query)
                        except Exception as e:
                            console.print(f"[red]Search failed: {e}[/]")
                            cands = []
                    if cands:
                        console.print(_cands_table(cands))
                    else:
                        console.print("[yellow]No EPUB releases found.[/]")
                    choices = [str(i) for i in range(1, len(cands) + 1)] + ["s", "r", "q"]
                    ans = Prompt.ask("Pick # / [s]kip / [r]e-search / [q]uit", choices=choices,
                                     show_choices=False)
                    if ans == "q":
                        return
                    if ans == "s":
                        svc.skip(r["item_id"])
                        break
                    if ans == "r":
                        query = Prompt.ask("New query", default=query)
                        continue
                    c = cands[int(ans) - 1]
                    svc.enqueue(r["item_id"], c.raw)

                    with console.status("Downloading…") as st:
                        def show(state, pct, msg, st=st):
                            st.update(f"{state} {'' if pct is None else f'{pct:.0f}%'} {msg}")
                        try:
                            placed = await svc.process(r["item_id"], show)
                            console.print(f"[green]Saved[/] {placed}")
                            done += 1
                        except Exception as e:
                            console.print(f"[red]Failed:[/] {e}")
                    break
            console.print(f"\nDone. {done} ebook(s) added.")
        finally:
            await svc.aclose()
    asyncio.run(go())


@app.command()
def presearch(count: int = typer.Option(100, help="How many missing books to search ahead"),
              rescan: bool = typer.Option(True, help="Rescan ABS first")):
    """Search the next N missing books in the background so they open instantly in the web UI.

    Safe to run from cron while the web UI is up (results go to the shared database).
    """
    from .core.presearch import PreSearcher

    async def go():
        svc = _service()
        try:
            if rescan:
                with console.status("Scanning Audiobookshelf…"):
                    await svc.scan()
            pre = PreSearcher(svc)
            targets = pre.targets(count)
            if not targets:
                console.print("Every missing book already has saved results.")
                return
            console.print(f"Pre-searching {len(targets)} book(s), one at a time…")
            task = asyncio.create_task(pre.run(targets))
            with console.status("") as st:
                while not task.done():
                    s = pre.status.to_dict()
                    extra = (f"waiting ~{s['cooldown_left']}s for rate-limit cooldown" if s["cooldown_left"]
                             else s["current"])
                    st.update(f"{s['done']}/{s['total']} {extra}")
                    await asyncio.sleep(1)
            s = await task
            console.print(f"Done: {s.done} searched, {s.failed} failed, {s.partial} incomplete.")
        finally:
            await svc.aclose()
    asyncio.run(go())


@app.command()
def web():
    """Start the web UI."""
    from .web.app import run as run_web
    run_web()


if __name__ == "__main__":
    app()
