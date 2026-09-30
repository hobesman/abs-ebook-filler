"""Recognising Anna's Archive / Shelfmark rate-limit messages and how long to back off."""

from __future__ import annotations

import re

_COOLDOWN_RE = re.compile(r"~\s*(\d+)\s*s\b")
DEFAULT_COOLDOWN = 90.0


def is_rate_limit(msg: str) -> bool:
    m = (msg or "").lower()
    return "429" in m or "rate-limit" in m or "rate limit" in m or "cooldown" in m


def rate_limit_wait(messages: list[str]) -> float | None:
    """Seconds to wait if any message is a rate-limit notice ("... for ~87s until the cooldown clears")."""
    for msg in messages:
        if is_rate_limit(msg):
            m = _COOLDOWN_RE.search(msg)
            return float(m.group(1)) if m else DEFAULT_COOLDOWN
    return None
