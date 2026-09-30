from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Book:
    item_id: str
    library_id: str
    title: str
    clean_title: str
    author: str
    path: str  # item folder as seen by the ABS container
    subtitle: str = ""
    series: str = ""
    isbn: str = ""
    asin: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Candidate:
    """One Shelfmark release, normalised for display. ``raw`` is sent back verbatim to download."""

    title: str
    author: str
    format: str
    size: str
    language: str
    source: str
    score: int = 0
    popularity: int = 0  # seeders (torrent) or download count (direct)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SearchResult:
    """EPUB candidates plus any per-source failures (e.g. "annas-archive.gl is rate-limited (429)")."""

    cands: list[Candidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    searched_at: float | None = None  # epoch seconds; older than "now" when served from the saved-search cache
    from_cache: bool = False

    def to_data(self) -> dict[str, Any]:
        return {"cands": [c.to_dict() for c in self.cands], "warnings": list(self.warnings)}

    @classmethod
    def from_data(cls, data: dict[str, Any], searched_at: float) -> "SearchResult":
        return cls([Candidate(**c) for c in data.get("cands") or []], list(data.get("warnings") or []),
                   searched_at=searched_at, from_cache=True)
