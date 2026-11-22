"""Unit tests for helpers that don't need the HTTP stack."""

from __future__ import annotations

import asyncio

import pytest

from tvdb_agent import keys, matching, orders
from tvdb_agent.cache import TTLCache
from tvdb_agent.config import ConfigError, build_settings
from tvdb_agent.i18n import to_tvdb_language


@pytest.mark.parametrize(
    "value,expected",
    [
        ("aired", "official"), ("Aired Order", "official"), ("tvdbAiring", "official"), ("official", "official"),
        ("DVD", "dvd"), ("tvdbDvd", "dvd"), ("dvd-order", "dvd"), ("absolute", "absolute"), ("abs", "absolute"),
        ("alt", "alternate"), ("altdvd", "altdvd"), ("alt2", "alttwo"), ("default", "default"), ("2", "dvd"),
        ("tmdbAiring", None), ("", None), (None, None), ("bogus", None),
    ],
)
def test_parse_order(value, expected):
    assert orders.parse_order(value) == expected


def test_series_orders_and_season_types():
    series = {
        "defaultSeasonType": 2,
        "seasonTypes": [{"id": 1, "type": "official", "name": "Aired Order"}, {"id": 2, "type": "dvd", "name": "DVD Order"}],
        "seasons": [
            {"number": 1, "type": {"id": 1, "type": "official", "name": "Aired Order"}},
            {"number": 1, "type": {"id": 2, "type": "dvd", "name": "DVD Order"}},
            {"number": 1, "type": {"id": 4, "type": "alternate", "name": "Alternate Order", "alternateName": "Netflix"}},
        ],
    }
    assert orders.series_default_order(series) == "dvd"
    assert orders.series_available_orders(series) == ["dvd", "official", "alternate"]
    st = orders.plex_season_types(series, first="official")
    assert [s["id"] for s in st] == ["tvdbAiring", "tvdbDvd", "tvdbAlternate"]
    assert st[2]["title"] == "TheTVDB (Alternate Order (Netflix))"


@pytest.mark.parametrize(
    "value,expected",
    [("en-US", "eng"), ("en", "eng"), ("de-DE", "deu"), ("pt-BR", "pt"), ("pt-PT", "por"), ("zh-TW", "zhtw"),
     ("zh-CN", "zho"), ("ja", "jpn"), ("eng", "eng"), ("", None), (None, None)],
)
def test_language_mapping(value, expected):
    assert to_tvdb_language(value) == expected


def test_keys_roundtrip():
    assert keys.parse_key(keys.show_key(5)) == keys.Key("show", 5)
    assert keys.parse_key(keys.season_key(5, "dvd", 0)) == keys.Key("season", 5, "dvd", season=0)
    assert keys.parse_key(keys.episode_key(5, "absolute", 77)) == keys.Key("episode", 5, "absolute", episode_id=77)
    assert keys.parse_key("series-x") is None
    assert keys.parse_key("../etc") is None


def test_parse_tags_and_guids():
    hints = keys.parse_tags("Show (2001) {imdb-tt123} [tvdbid-42] {tmdb-9}")
    assert [(h.source, h.value) for h in hints] == [("tvdb", "42"), ("imdb", "tt123"), ("tmdb", "9")]
    assert keys.strip_tags("Show {tvdb-42}") == "Show"
    own = keys.parse_guid("tv.plex.agents.custom.foo.bar://show/series-42")
    assert (own.source, own.value, own.kind) == ("own", "series-42", "show")
    assert keys.parse_guid("themoviedb://99").source == "tmdb"
    assert keys.parse_guid("plex://show/abc") is None


def test_matching_scores():
    assert matching.normalize("The Office (US)") == "office us"
    assert matching.normalize("Amélie & Co.") == "amelie and co"
    assert matching.split_year("Doctor Who (2005)") == ("Doctor Who", 2005)
    exact = matching.score("Doctor Who", 2005, ["Doctor Who"], 2005)
    wrong_year = matching.score("Doctor Who", 2005, ["Doctor Who"], 1963)
    other = matching.score("Doctor Who", 2005, ["Doctor Foster"], 2005)
    assert exact == 100 and exact > wrong_year and exact > other


def test_config_validation():
    with pytest.raises(ConfigError):
        build_settings({"provider": {"identifier": "com.example.tvdb"}}, env=False)
    with pytest.raises(ConfigError):
        build_settings({"profiles": {"bad-name": {}}}, env=False)
    with pytest.raises(ConfigError):
        build_settings({"profiles": {"global": {}}}, env=False)
    with pytest.raises(ConfigError):
        build_settings({"show_overrides": {"123": "sideways"}}, env=False)
    with pytest.raises(ConfigError):
        build_settings({"profiles": {"x": {"episode_order": "nope"}}}, env=False)
    with pytest.raises(ConfigError):
        build_settings({"profiles": {"x": {"identifier": "tv.plex.agents.custom.tvdbcustomagent"}}}, env=False)
    s = build_settings({"show_overrides": {"123": "Aired", 456: "tvdbDvd"}, "defaults": {"language": "fr-FR"}}, env=False)
    assert s.show_overrides == {123: "official", 456: "dvd"}
    assert s.language == "fra"
    assert s.profile("dvd").identifier == "tv.plex.agents.custom.tvdbcustomagent.dvd"
    s = build_settings({"provider": {"builtin_profiles": False}}, env=False)
    assert s.profile("dvd") is None


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("TVDB_API_KEY", "from-env")
    monkeypatch.setenv("DEFAULT_EPISODE_ORDER", "dvd")
    monkeypatch.setenv("PROVIDER_IDENTIFIER", "tv.plex.agents.custom.me.tvdb")
    s = build_settings({"tvdb": {"api_key": "from-file"}})
    assert s.tvdb_api_key == "from-env"
    assert s.root.episode_order == "dvd"
    assert s.profile("absolute").identifier == "tv.plex.agents.custom.me.tvdb.absolute"


def test_cache_coalesces_concurrent_fetches():
    cache = TTLCache(max_entries=2)
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return calls

    async def run():
        return await asyncio.gather(*(cache.get_or_fetch("k", 60, fetch) for _ in range(5)))

    assert asyncio.run(run()) == [1] * 5
    assert calls == 1
    cache.set("a", 1, 60)
    cache.set("b", 2, 60)
    assert len(cache) == 2 and cache.get("k") == (False, None)  # LRU eviction


def test_cache_failure_is_not_cached():
    cache = TTLCache()
    attempts = 0

    async def boom():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("x")

    async def run():
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cache.get_or_fetch("k", 60, boom)

    asyncio.run(run())
    assert attempts == 2


def test_example_config_loads():
    from pathlib import Path

    from tvdb_agent.config import load_settings

    path = Path(__file__).resolve().parent.parent / "config.example.yaml"
    s = load_settings(path, env=False)
    assert s.tvdb_api_key == "your-tvdb-v4-api-key"
    assert s.profile("anime").episode_order == "absolute"
    assert s.profile("anime").identifier == "tv.plex.agents.custom.tvdbcustomagent.anime"
