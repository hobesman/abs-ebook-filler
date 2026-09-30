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
