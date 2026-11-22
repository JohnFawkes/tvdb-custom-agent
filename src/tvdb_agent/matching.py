"""Title normalisation and match scoring."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable

_YEAR_SUFFIX = re.compile(r"\s*[(\[]\s*(\d{4})\s*[)\]]\s*$")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")
_LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+")


def split_year(title: str) -> tuple[str, int | None]:
    """``"Doctor Who (2005)"`` -> ``("Doctor Who", 2005)``."""
    m = _YEAR_SUFFIX.search(title)
    if not m:
        return title.strip(), None
    return title[: m.start()].strip(), int(m.group(1))


def normalize(title: str | None) -> str:
    if not title:
        return ""
    text = unicodedata.normalize("NFKD", title)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = _NON_WORD.sub(" ", text)
    text = _SPACES.sub(" ", text).strip()
    text = _LEADING_ARTICLE.sub("", text)
    return text


def title_similarity(query: str, candidates: Iterable[str]) -> float:
    """Best similarity (0..1) between ``query`` and any candidate title."""
    q = normalize(query)
    if not q:
        return 0.0
    best = 0.0
    for cand in candidates:
        c = normalize(cand)
        if not c:
            continue
        if c == q:
            return 1.0
        ratio = SequenceMatcher(None, q, c).ratio()
        # A candidate that merely appends a year/country ("Shameless US").
        if c.startswith(q + " ") or q.startswith(c + " "):
            ratio = max(ratio, 0.9)
        best = max(best, ratio)
    return best


def score(query: str, year: int | None, candidates: Iterable[str], cand_year: int | None, rank: int = 0) -> int:
    """Return a 0..100 confidence score."""
    sim = title_similarity(query, candidates)
    points = sim * 85.0
    if year and cand_year:
        diff = abs(year - cand_year)
        if diff == 0:
            points += 15
        elif diff == 1:
            points += 7
        else:
            points -= min(30, diff * 5)
    elif not year:
        points += 5  # no year to compare: don't punish
    points -= min(rank, 10) * 0.5  # TVDB's own relevance as a tie breaker
    return max(0, min(100, int(round(points))))


def year_of(value: object) -> int | None:
    if value is None:
        return None
    m = re.match(r"^\s*(\d{4})", str(value))
    return int(m.group(1)) if m else None
