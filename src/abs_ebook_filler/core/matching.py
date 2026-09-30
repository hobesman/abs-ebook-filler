"""Title cleaning, query building and candidate scoring."""

from __future__ import annotations

import re

from rapidfuzz import fuzz

_TRIM_CHARS = " \t\r\n-–—:.,_"

_TAG_RE = re.compile(
    r"[\(\[]\s*(?:un)?abridged\s*[\)\]]"
    r"|[\(\[]\s*dramati[sz]ed(?:\s+adaptation)?\s*[\)\]]"
    r"|[\(\[]\s*audiobook\s*[\)\]]",
    re.IGNORECASE,
)
_BRACKET_NUM_RE = re.compile(r"\[\s*\d+(?:\.\d+)?\s*\]")
_NUM_SPACE_DASH_RE = re.compile(r"\d+\s-\s")
_NUM_DASH_RE = re.compile(r"\d+-")
_TWO_DIGIT_RE = re.compile(r"(?<![\d.])\b\d{2}\b(?![\d.])")


def _trim(s: str) -> str:
    return s.strip(_TRIM_CHARS)


def _acceptable(remainder: str) -> bool:
    """Guard: a rule only applies if it leaves something title-like behind."""
    r = _trim(remainder)
    return len(r) > 1 and any(c.isalpha() for c in r)


def _after_last(pattern: re.Pattern[str], title: str) -> str:
    matches = list(pattern.finditer(title))
    if not matches:
        return title
    remainder = title[matches[-1].end():]
    return _trim(remainder) if _acceptable(remainder) else title


def _after_first(pattern: re.Pattern[str], title: str) -> str:
    m = pattern.search(title)
    if not m:
        return title
    remainder = title[m.end():]
    return _trim(remainder) if _acceptable(remainder) else title


def clean_title(raw: str) -> str:
    """Strip series-name/number prefixes that ABS titles often carry.

    Rules, applied in order:
      1. ``[N]``          -> keep text after the bracketed number
      2. ``N - ``         -> keep text after the (last) number-space-dash-space
      3. ``N-``           -> keep text after the (last) number-dash
      4. standalone ``NN`` (two digits, leading zeros ok) -> keep text after it
    A rule is skipped if it would leave nothing title-like behind.
    """
    if not raw:
        return ""
    title = _trim(re.sub(r"\s+", " ", _TAG_RE.sub(" ", raw)))
    title = _after_last(_BRACKET_NUM_RE, title)
    title = _after_last(_NUM_SPACE_DASH_RE, title)
    title = _after_last(_NUM_DASH_RE, title)
    title = _after_first(_TWO_DIGIT_RE, title)
    return _trim(title) or _trim(raw)


def primary_author(author: str | None) -> str:
    if not author:
        return ""
    return re.split(r"\s*(?:,|&|;|\band\b)\s*", author, maxsplit=1)[0].strip()


def build_query(clean: str, author: str | None) -> str:
    return " ".join(p for p in (clean, primary_author(author)) if p).strip()


def _norm(s: str | None) -> str:
    return re.sub(r"[^\w\s]", " ", (s or "").lower()).strip()


def _title_similarity(cand: str, target: str) -> float:
    # token_set alone scores 100 whenever the target's words are a subset of the candidate
    # ("Dune" vs "God Emperor of Dune"), so blend in token_sort, which penalises extra words.
    return 0.5 * fuzz.token_set_ratio(cand, target) + 0.5 * fuzz.token_sort_ratio(cand, target)


def score(
    cand_title: str | None,
    cand_author: str | None,
    clean: str,
    raw: str,
    author: str | None,
) -> int:
    """0-100 similarity of a release to the ABS book."""
    full = cand_title or ""
    # Also compare the part before a subtitle colon: "Dune : Now a major film..." -> "Dune".
    variants = {_norm(full), _norm(full.split(":", 1)[0])} - {""}
    targets = {_norm(clean), _norm(raw)} - {""}
    title_score = max(
        (_title_similarity(v, t) for v in variants for t in targets), default=0.0
    )
    ca, aa = _norm(cand_author), _norm(author)
    if not ca or not aa:
        return round(title_score)
    author_score = max(
        fuzz.token_set_ratio(ca, aa),
        fuzz.token_set_ratio(ca, _norm(primary_author(author))),
    )
    return round(0.7 * title_score + 0.3 * author_score)
