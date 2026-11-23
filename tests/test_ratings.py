"""IMDb ratings from the public dataset, and the optional TMDB lookups."""

from __future__ import annotations

import asyncio
import gzip
import io
import time
from urllib.parse import parse_qs, urlparse

import httpx

from conftest import items, one
from tvdb_agent.imdb import IMDbRatings, parse_ratings

# A tiny title.ratings.tsv. Like the real file it is ordered by tconst as
# text, so ids of different lengths are not in numeric order.
DATASET_ROWS = [
    ("tt0000005", "6.2", "2700"),
    ("tt10000001", "5.0", "10"),   # sorts before tt7000001 as text
    ("tt3000000", "7.3", "900"),    # Space Police (found through TMDB)
    ("tt7000001", "8.1", "52000"),  # Galaxy Patrol
    ("tt809001", "7.5", "1200"),    # Galaxy Patrol episode 9001
    ("tt809002", "9.0", "800"),
    ("tt8800001", "6.6", "55"),     # Space Police episode 8001 (via TMDB)
]


def dataset_bytes(rows=DATASET_ROWS) -> bytes:
    text = "tconst\taverageRating\tnumVotes\n" + "".join("\t".join(r) + "\n" for r in rows)
    return gzip.compress(text.encode())


class FakeIMDb:
    def __init__(self, body: bytes | None = None) -> None:
        self.body = body or dataset_bytes()
        self.requests: list[httpx.Request] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=self.body,
                              headers={"ETag": '"v1"', "Last-Modified": "Sun, 22 Nov 2026 00:46:28 GMT"})


class FakeTMDB:
    """/find by TVDB id, /tv/{id} with external_ids, episode external_ids."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"status_code": 7, "status_message": "Invalid API key"})
        url = urlparse(str(request.url))
        path = url.path.removeprefix("/3")
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        empty = {"movie_results": [], "person_results": [], "tv_results": [], "tv_episode_results": [], "tv_season_results": []}
        if path.startswith("/find/") and q.get("external_source") == "tvdb_id":
            tvdb_id = int(path.rsplit("/", 1)[1])
            if tvdb_id == 1000:
                return httpx.Response(200, json=empty | {"tv_results": [{"id": 70001, "vote_average": 7.84, "vote_count": 120}]})
            if tvdb_id == 3000:
                return httpx.Response(200, json=empty | {"tv_results": [{"id": 99, "vote_average": 6.0, "vote_count": 3}]})
            if 9001 <= tvdb_id <= 9006:
                return httpx.Response(200, json=empty | {"tv_episode_results": [{
                    "id": 50000 + tvdb_id, "show_id": 70001, "season_number": 1, "episode_number": tvdb_id - 9000,
                    "vote_average": 8.25, "vote_count": 12}]})
            if tvdb_id == 8001:
                return httpx.Response(200, json=empty | {"tv_episode_results": [{
                    "id": 88001, "show_id": 99, "season_number": 1, "episode_number": 1, "vote_average": 0, "vote_count": 0}]})
            return httpx.Response(200, json=empty)
        if path == "/tv/70001":
            return httpx.Response(200, json={"id": 70001, "vote_average": 7.84, "vote_count": 120,
                                             "external_ids": {"imdb_id": "tt7000001"}})
        if path == "/tv/99":
            return httpx.Response(200, json={"id": 99, "vote_average": 6.0, "vote_count": 3,
                                             "external_ids": {"imdb_id": "tt3000000"}})
        if path == "/tv/99/season/1/episode/1/external_ids":
            return httpx.Response(200, json={"id": 88001, "imdb_id": "tt8800001", "tvdb_id": 8001})
        return httpx.Response(404, json={"status_code": 34})


def wait_for_imdb(client, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get("/health").json().get("imdbRatings") or {}
        if status.get("loaded"):
            return status
        time.sleep(0.05)
    raise AssertionError("IMDb dataset never loaded")


def ratings_of(md):
    return {r["image"]: r["value"] for r in md.get("Rating", [])}


def guids_of(md):
    return [g["id"] for g in md.get("Guid", [])]


# ------------------------------------------------------------- dataset
def test_parse_ratings_merges_text_ordered_ids():
    keys = parse_ratings(io.BytesIO(gzip.decompress(dataset_bytes())))
    assert len(keys) == len(DATASET_ROWS)
    assert list(keys) == sorted(keys)


def test_lookup_and_conditional_refresh(tmp_path):
    fake = FakeIMDb()
    imdb = IMDbRatings(tmp_path, transport=fake.transport)

    async def run():
        assert await imdb.refresh() is True
        assert imdb.rating("tt7000001") == 8.1
        assert imdb.rating("tt10000001") == 5.0
        assert imdb.rating("tt0000005") == 6.2
        assert imdb.rating("tt1234567") is None
        assert imdb.rating("nm0000001") is None and imdb.rating(None) is None
        # The second refresh is conditional and the server answers 304.
        assert await imdb.refresh() is False
        assert fake.requests[-1].headers["If-None-Match"] == '"v1"'
        await imdb.aclose()

    asyncio.run(run())
    assert (tmp_path / "imdb" / "title.ratings.tsv.gz").is_file()
    assert imdb.status() == {"loaded": True, "titles": 7, "datasetDate": "Sun, 22 Nov 2026 00:46:28 GMT"}

    # A new process loads the cached copy without downloading again.
    again = IMDbRatings(tmp_path, transport=FakeIMDb().transport)

    async def reload():
        again.start()
        for _ in range(100):
            if again.loaded:
                break
            await asyncio.sleep(0.01)
        await again.aclose()

    asyncio.run(reload())
    assert again.rating("tt809001") == 7.5


def test_unwritable_data_dir_keeps_ratings_in_memory(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    imdb = IMDbRatings(blocker / "data", transport=FakeIMDb().transport)

    async def run():
        await imdb.refresh()
        await imdb.aclose()

    asyncio.run(run())
    assert imdb.rating("tt7000001") == 8.1


# ---------------------------------------------------- in Plex responses
def test_imdb_ratings_on_shows_and_episodes(make_client):
    c = make_client({"imdb": {"ratings": True}}, {"imdb_transport": FakeIMDb().transport})
    wait_for_imdb(c)
    show = one(c.get("/library/metadata/series-1000"))
    assert ratings_of(show) == {"imdb://image.rating": 8.1}
    assert show["Rating"][0] == {"image": "imdb://image.rating", "type": "audience", "value": 8.1}
    season = one(c.get("/library/metadata/season-1000-official-1", params={"includeChildren": 1}))
    eps = {e["ratingKey"]: e for e in season["Children"]["Metadata"]}
    assert ratings_of(eps["episode-1000-official-9001"]) == {"imdb://image.rating": 7.5}
    assert ratings_of(eps["episode-1000-official-9002"]) == {"imdb://image.rating": 9.0}
    assert "Rating" not in eps["episode-1000-official-9003"]  # not in the dataset
    single = one(c.get("/library/metadata/episode-1000-official-9001"))
    assert ratings_of(single) == {"imdb://image.rating": 7.5}


def test_tmdb_ratings_and_missing_ids(make_client):
    tmdb = FakeTMDB()
    c = make_client(
        {"imdb": {"ratings": True}, "tmdb": {"api_key": "eyJ-test-token"}},
        {"imdb_transport": FakeIMDb().transport, "tmdb_transport": tmdb.transport},
    )
    wait_for_imdb(c)
    show = one(c.get("/library/metadata/series-1000"))
    assert guids_of(show) == ["tvdb://1000", "imdb://tt7000001", "tmdb://70001"]
    assert ratings_of(show) == {"imdb://image.rating": 8.1, "themoviedb://image.rating": 7.8}
    assert tmdb.requests[0].headers["Authorization"] == "Bearer eyJ-test-token"

    # Space Police has no ids on TVDB: TMDB finds the show by its TVDB id,
    # and the IMDb id it returns unlocks the IMDb rating.
    sp = one(c.get("/library/metadata/series-3000"))
    assert guids_of(sp) == ["tvdb://3000", "tmdb://99", "imdb://tt3000000"]
    assert ratings_of(sp) == {"imdb://image.rating": 7.3, "themoviedb://image.rating": 6.0}

    season = one(c.get("/library/metadata/season-1000-official-1", params={"includeChildren": 1}))
    ep = season["Children"]["Metadata"][0]
    assert guids_of(ep) == ["tvdb://9001", "imdb://tt809001", "tmdb://59001"]
    assert ratings_of(ep) == {"imdb://image.rating": 7.5, "themoviedb://image.rating": 8.2}

    # The episode IMDb id TVDB lacks comes from TMDB's external ids.
    sp_eps = items(c.get("/library/metadata/season-3000-official-1/children"))
    assert guids_of(sp_eps[0]) == ["tvdb://8001", "tmdb://88001", "imdb://tt8800001"]
    assert ratings_of(sp_eps[0]) == {"imdb://image.rating": 6.6}  # TMDB has no votes

    # Lookups are cached.
    before = len(tmdb.requests)
    c.get("/library/metadata/season-1000-official-1", params={"includeChildren": 1})
    assert len(tmdb.requests) == before


def test_tmdb_v3_key_and_bad_key(make_client):
    tmdb = FakeTMDB()
    tmdb.status = 401
    c = make_client({"tmdb": {"api_key": "0123456789abcdef"}}, {"tmdb_transport": tmdb.transport})
    show = one(c.get("/library/metadata/series-1000"))
    assert "Rating" not in show and guids_of(show) == ["tvdb://1000", "imdb://tt7000001", "tmdb://70001"]
    assert parse_qs(urlparse(str(tmdb.requests[0].url)).query)["api_key"] == ["0123456789abcdef"]
    # After a 401 the agent stops asking TMDB for a while.
    calls = len(tmdb.requests)
    one(c.get("/library/metadata/series-3000"))
    assert len(tmdb.requests) == calls


def test_ratings_off_by_default_in_tests(client):
    assert client.get("/health").json()["imdbRatings"] is None
    assert "Rating" not in one(client.get("/library/metadata/series-1000"))
