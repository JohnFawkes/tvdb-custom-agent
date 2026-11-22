"""ratingKey / GUID construction and parsing.

ratingKeys (``[A-Za-z0-9_-]`` only, as required by Plex):

* show     ``series-<seriesId>``
* season   ``season-<seriesId>-<order>-<seasonNumber>``
* episode  ``episode-<seriesId>-<order>-<episodeId>``

The ordering is part of season and episode keys, so once Plex has stored a
season it keeps returning the same numbering, whichever URL it is fetched
through.

GUIDs follow ``{identifier}://{type}/{ratingKey}``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SERIES = re.compile(r"^series-(\d+)$")
_SEASON = re.compile(r"^season-(\d+)-([a-z0-9]+)-(\d+)$")
_EPISODE = re.compile(r"^episode-(\d+)-([a-z0-9]+)-(\d+)$")


@dataclass(frozen=True)
class Key:
    kind: str  # "show" | "season" | "episode"
    series_id: int
    order: str | None = None
    season: int | None = None
    episode_id: int | None = None


def show_key(series_id: int) -> str:
    return f"series-{int(series_id)}"


def season_key(series_id: int, order: str, season: int) -> str:
    return f"season-{int(series_id)}-{order}-{int(season)}"


def episode_key(series_id: int, order: str, episode_id: int) -> str:
    return f"episode-{int(series_id)}-{order}-{int(episode_id)}"


def parse_key(rating_key: str) -> Key | None:
    m = _SERIES.match(rating_key)
    if m:
        return Key("show", int(m.group(1)))
    m = _SEASON.match(rating_key)
    if m:
        return Key("season", int(m.group(1)), m.group(2), season=int(m.group(3)))
    m = _EPISODE.match(rating_key)
    if m:
        return Key("episode", int(m.group(1)), m.group(2), episode_id=int(m.group(3)))
    return None


def guid(identifier: str, kind: str, rating_key: str) -> str:
    return f"{identifier}://{kind}/{rating_key}"


# --------------------------------------------------------------------- hints
_OWN_GUID = re.compile(r"^tv\.plex\.agents\.custom\.[A-Za-z0-9.]+://(show|season|episode)/([A-Za-z0-9_-]+)")
_EXTERNAL_GUID = re.compile(r"^(tvdb|imdb|tmdb|themoviedb)://([A-Za-z0-9]+)", re.I)
# Plex/Sonarr style folder tags: "Show (2005) {tvdb-12345}", "[tvdbid-12345]",
# "{imdb-tt0123456}", "{tmdb-1234}".
_TAG = re.compile(r"[\[{(]\s*(tvdb|tvdbid|imdb|imdbid|tmdb|tmdbid)\s*[-=: ]\s*([A-Za-z0-9]+)\s*[\]})]", re.I)


@dataclass
class IdHint:
    source: str  # "own" | "tvdb" | "imdb" | "tmdb"
    value: str
    kind: str | None = None  # for own guids: show/season/episode


def parse_guid(value: str | None) -> IdHint | None:
    if not value:
        return None
    value = value.strip()
    m = _OWN_GUID.match(value)
    if m:
        return IdHint("own", m.group(2), m.group(1))
    m = _EXTERNAL_GUID.match(value)
    if m:
        src = m.group(1).lower()
        return IdHint("tmdb" if src == "themoviedb" else src, m.group(2))
    return None


def parse_tags(text: str | None) -> list[IdHint]:
    """Extract ``{tvdb-123}``-style ids from a title or file path."""
    hints: list[IdHint] = []
    if not text:
        return hints
    for m in _TAG.finditer(text):
        src = m.group(1).lower().removesuffix("id")
        hints.append(IdHint(src, m.group(2)))
    # Prefer tvdb tags, then imdb, then tmdb.
    rank = {"tvdb": 0, "imdb": 1, "tmdb": 2}
    hints.sort(key=lambda h: rank.get(h.source, 9))
    return hints


def strip_tags(text: str) -> str:
    return _TAG.sub("", text).strip()
