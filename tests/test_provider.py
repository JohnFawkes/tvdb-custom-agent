"""End-to-end tests of the Plex provider API against the fake TVDB."""

from __future__ import annotations

from conftest import items, one

ROOT_ID = "tv.plex.agents.custom.tvdbcustomagent"


# ------------------------------------------------------------------ root
def test_root_provider(client):
    body = client.get("/").json()["MediaProvider"]
    assert body["identifier"] == ROOT_ID
    assert [t["type"] for t in body["Types"]] == [2, 3, 4]
    assert all(t["Scheme"] == [{"scheme": ROOT_ID}] for t in body["Types"])
    features = {f["type"]: f["key"] for f in body["Feature"]}
    assert features == {"metadata": "/library/metadata", "match": "/library/metadata/matches"}


def test_profile_root_has_own_identifier_and_prefixed_keys(client):
    for path in ("/profiles/dvd", "/profiles/dvd/"):
        body = client.get(path).json()["MediaProvider"]
        assert body["identifier"] == ROOT_ID + ".dvd"
        assert body["Types"][0]["Scheme"][0]["scheme"] == ROOT_ID + ".dvd"
        features = {f["type"]: f["key"] for f in body["Feature"]}
        assert features["metadata"] == "/profiles/dvd/library/metadata"
    assert client.get("/profiles/nope").status_code == 404


def test_repeated_profile_prefix_is_collapsed(client):
    md = one(client.get("/profiles/dvd/profiles/dvd/library/metadata/series-1000"))
    assert md["guid"] == f"{ROOT_ID}.dvd://show/series-1000"


def test_profiles_listing(make_client):
    c = make_client({"profiles": {"anime": {"episode_order": "absolute", "language": "ja"}}})
    profiles = {p["name"]: p for p in c.get("/profiles").json()["profiles"]}
    assert profiles[""]["identifier"] == ROOT_ID
    assert profiles["dvd"]["builtin"] is True
    assert profiles["anime"]["episodeOrder"] == "absolute"
    assert profiles["anime"]["language"] == "jpn"
    assert profiles["anime"]["path"] == "/profiles/anime"


# ------------------------------------------------------------------ show
def test_show_metadata(client):
    resp = client.get("/library/metadata/series-1000")
    container = resp.json()["MediaContainer"]
    assert container["identifier"] == ROOT_ID
    assert container["size"] == container["totalSize"] == 1
    md = container["Metadata"][0]
    assert md["ratingKey"] == "series-1000"
    assert md["key"] == "/library/metadata/series-1000/children"
    assert md["guid"] == f"{ROOT_ID}://show/series-1000"
    assert md["type"] == "show"
    assert md["title"] == "Galaxy Patrol"
    assert "originalTitle" not in md
    assert md["originallyAvailableAt"] == "2001-09-10"
    assert md["year"] == 2001
    assert md["summary"] == "A patrol ship keeps the peace."
    assert md["contentRating"] == "TV-PG"
    assert md["studio"] == "Nova Studios"
    assert md["duration"] == 42 * 60_000
    assert md["theme"] == "https://tvthemes.plexapp.com/1000.mp3"
    assert md["thumb"].endswith("1000-eng.jpg")
    assert md["art"].endswith("fanart/1000-1.jpg")  # textless background preferred
    assert [g["tag"] for g in md["Genre"]] == ["Science Fiction", "Drama"]
    assert [g["id"] for g in md["Guid"]] == ["tvdb://1000", "imdb://tt7000001", "tmdb://70001"]
    assert md["Network"] == [{"tag": "SpaceNet"}]
    assert md["Studio"] == [{"tag": "Nova Studios"}]
    assert md["Country"] == [{"tag": "United States of America"}]
    assert md["Role"][0] == {
        "tag": "Ann Actor", "role": "Captain Nova", "order": 1,
        "thumb": "https://artworks.thetvdb.com/banners/person/1.jpg",
    }
    assert md["Director"][0]["tag"] == "Dee Rector"
    assert md["Writer"][0] == {"tag": "Cree Ator", "role": "Creator", "order": 1}
    assert md["Producer"][0]["tag"] == "Pro Ducer"
    image_types = [i["type"] for i in md["Image"]]
    assert image_types == ["coverPoster", "background", "clearLogo"]
    assert [s["id"] for s in md["SeasonType"]] == ["tvdbAiring", "tvdbDvd", "tvdbAbsolute"]
    assert md["SeasonType"][0] == {"id": "tvdbAiring", "source": "tvdb", "tag": "Aired", "title": "TheTVDB (Aired)"}
    assert "Children" not in md


def test_show_include_children(client):
    md = one(client.get("/library/metadata/series-1000", params={"includeChildren": 1}))
    children = md["Children"]
    assert children["size"] == 3
    assert [c["index"] for c in children["Metadata"]] == [0, 1, 2]
    assert children["Metadata"][0]["title"] == "Specials"
    assert children["Metadata"][1]["ratingKey"] == "season-1000-official-1"


def test_show_include_children_with_plex_episode_order(client):
    md = one(client.get("/library/metadata/series-1000", params={"includeChildren": 1, "episodeOrder": "tvdbDvd"}))
    assert [c["ratingKey"] for c in md["Children"]["Metadata"]] == ["season-1000-dvd-1", "season-1000-dvd-2"]


def test_theme_music_can_be_disabled(make_client):
    c = make_client({"defaults": {"theme_music": False}})
    assert "theme" not in one(c.get("/library/metadata/series-1000"))


# --------------------------------------------------------------- paging
def test_children_paging(client):
    resp = client.get("/library/metadata/series-1000/children")
    assert [c["index"] for c in items(resp)] == [0, 1, 2]
    resp = client.get(
        "/library/metadata/series-1000/children",
        headers={"X-Plex-Container-Start": "1", "X-Plex-Container-Size": "1"},
    )
    container = resp.json()["MediaContainer"]
    assert container["offset"] == 1 and container["size"] == 1 and container["totalSize"] == 3
    assert container["Metadata"][0]["index"] == 1
    assert resp.headers["X-Plex-Container-Start"] == "1"
    assert resp.headers["X-Plex-Container-Total-Size"] == "3"


def test_paging_via_query_params_and_size_zero(client):
    resp = client.get("/library/metadata/series-1000/grandchildren", params={"X-Plex-Container-Size": 0})
    container = resp.json()["MediaContainer"]
    assert container["size"] == 0 and container["totalSize"] == 7


def test_grandchildren_default_page_is_20(client, fake):
    eps = items(client.get("/library/metadata/series-1000/grandchildren"))
    assert len(eps) == 7
    assert [(e["parentIndex"], e["index"]) for e in eps][:3] == [(0, 1), (1, 1), (1, 2)]
    # TVDB pagination (page size 4 in the fake) was followed.
    assert fake.count("/series/1000/episodes/official?page=1") == 1


def test_grandchildren_page(client):
    resp = client.get(
        "/library/metadata/series-1000/grandchildren",
        headers={"X-Plex-Container-Start": "5", "X-Plex-Container-Size": "5"},
    )
    container = resp.json()["MediaContainer"]
    assert container["size"] == 2 and container["totalSize"] == 7 and container["offset"] == 5


# --------------------------------------------------------- episode order
def test_plex_episode_order_param(client):
    eps = items(client.get("/library/metadata/series-1000/grandchildren", params={"episodeOrder": "tvdbDvd"}))
    assert eps[0]["ratingKey"] == "episode-1000-dvd-9002"
    assert (eps[0]["parentIndex"], eps[0]["index"]) == (1, 1)
    assert eps[0]["parentRatingKey"] == "season-1000-dvd-1"
    assert all("-dvd-" in e["ratingKey"] for e in eps)
    assert len(eps) == 6  # the special is not part of the DVD order


def test_plex_episode_order_is_strict(client):
    # Space Police has no DVD order -> Plex asked for it explicitly -> no seasons.
    assert items(client.get("/library/metadata/series-3000/children", params={"episodeOrder": "tvdbDvd"})) == []


def test_unknown_plex_episode_order_is_ignored(client):
    eps = items(client.get("/library/metadata/series-1000/grandchildren", params={"episodeOrder": "tmdbAiring"}))
    assert eps[1]["ratingKey"] == "episode-1000-official-9001"


def test_dvd_profile(client):
    eps = items(client.get("/profiles/dvd/library/metadata/series-1000/grandchildren"))
    first = eps[0]
    assert first["ratingKey"] == "episode-1000-dvd-9002"
    assert first["guid"] == f"{ROOT_ID}.dvd://episode/episode-1000-dvd-9002"
    assert first["key"] == "/profiles/dvd/library/metadata/episode-1000-dvd-9002"
    assert first["grandparentKey"] == "/profiles/dvd/library/metadata/series-1000"
    show = one(client.get("/profiles/dvd/library/metadata/series-1000"))
    assert show["SeasonType"][0]["id"] == "tvdbDvd"  # profile order listed first


def test_absolute_profile(client):
    eps = items(client.get("/profiles/absolute/library/metadata/series-1000/grandchildren"))
    assert [(e["parentIndex"], e["index"]) for e in eps] == [(1, n) for n in range(1, 7)]


def test_profile_falls_back_when_show_lacks_order(client):
    eps = items(client.get("/profiles/dvd/library/metadata/series-3000/grandchildren"))
    assert [e["ratingKey"] for e in eps] == ["episode-3000-official-8001", "episode-3000-official-8002"]


def test_profile_without_fallback(make_client):
    c = make_client({"profiles": {"strictdvd": {"episode_order": "dvd", "fallback_to_default_order": False}}})
    assert items(c.get("/profiles/strictdvd/library/metadata/series-3000/grandchildren")) == []


def test_profile_can_ignore_plex_episode_order(make_client):
    c = make_client({"profiles": {"dvdonly": {"episode_order": "dvd", "honor_plex_episode_order": False}}})
    eps = items(c.get("/profiles/dvdonly/library/metadata/series-1000/grandchildren", params={"episodeOrder": "tvdbAiring"}))
    assert eps[0]["ratingKey"] == "episode-1000-dvd-9002"


def test_config_show_override_beats_plex_order(make_client):
    c = make_client({"show_overrides": {1000: "dvd"}})
    eps = items(c.get("/library/metadata/series-1000/grandchildren", params={"episodeOrder": "tvdbAiring"}))
    assert eps[0]["ratingKey"] == "episode-1000-dvd-9002"
    # Other shows are untouched.
    eps = items(c.get("/library/metadata/series-3000/grandchildren"))
    assert eps[0]["ratingKey"] == "episode-3000-official-8001"


def test_profile_show_override(make_client):
    c = make_client({"profiles": {"anime": {"episode_order": "absolute", "show_overrides": {"1000": "aired"}}}})
    eps = items(c.get("/profiles/anime/library/metadata/series-1000/grandchildren"))
    assert eps[0]["ratingKey"] == "episode-1000-official-9100"
    eps = items(c.get("/profiles/anime/library/metadata/series-3000/grandchildren"))
    assert eps[0]["ratingKey"] == "episode-3000-absolute-8001"


def test_admin_overrides_roundtrip(client, tmp_path):
    resp = client.put("/admin/api/overrides/global/1000", json={"order": "absolute"})
    assert resp.status_code == 200 and resp.json()["order"] == "absolute"
    eps = items(client.get("/library/metadata/series-1000/grandchildren"))
    assert all(e["parentIndex"] == 1 for e in eps) and len(eps) == 6
    assert (tmp_path / "overrides.json").is_file()

    # Profile-scoped override wins over the global one for that profile only.
    assert client.put("/admin/api/overrides/dvd/1000", json={"order": "tvdbAiring"}).status_code == 200
    assert items(client.get("/profiles/dvd/library/metadata/series-1000/grandchildren"))[0]["parentIndex"] == 0
    listing = client.get("/admin/api/overrides").json()["overrides"]
    assert {(o["scope"], o["order"]) for o in listing} == {("*", "absolute"), ("dvd", "official")}

    assert client.delete("/admin/api/overrides/global/1000").status_code == 204
    assert client.delete("/admin/api/overrides/global/1000").status_code == 404
    eps = items(client.get("/library/metadata/series-1000/grandchildren"))
    assert eps[0]["ratingKey"] == "episode-1000-official-9100"


def test_admin_override_validation(client):
    assert client.put("/admin/api/overrides/global/1000", json={"order": "bogus"}).status_code == 400
    assert client.put("/admin/api/overrides/nosuchprofile/1000", json={"order": "dvd"}).status_code == 404
    assert client.put("/admin/api/overrides/global/1000", json={}).status_code == 400


def test_admin_token_required(make_client):
    c = make_client({"server": {"admin_token": "s3cret"}})
    assert c.get("/admin/api/overrides").status_code == 401
    assert c.get("/admin/api/overrides", headers={"X-Admin-Token": "s3cret"}).status_code == 200
    assert c.get("/admin/api/overrides", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    # Provider endpoints never need the admin token.
    assert c.get("/").status_code == 200


def test_admin_series_and_search(client):
    data = client.get("/admin/api/series/1000").json()
    assert data["title"] == "Galaxy Patrol"
    assert data["defaultOrder"] == "official"
    assert {o["slug"]: o["episodes"] for o in data["orders"]} == {"official": 7, "dvd": 6, "absolute": 6}
    effective = {e["profile"]: e["order"] for e in data["effective"]}
    assert effective[""] == "official" and effective["dvd"] == "dvd"
    results = client.get("/admin/api/search", params={"q": "Galaxy Patrol"}).json()["results"]
    assert results[0]["seriesId"] == 1000
    assert client.get("/admin").status_code == 200


# ----------------------------------------------------------- seasons
def test_season_metadata(client):
    md = one(client.get("/library/metadata/season-1000-official-1"))
    assert md["type"] == "season"
    assert md["title"] == "Season 1"
    assert md["index"] == 1
    assert md["key"] == "/library/metadata/season-1000-official-1/children"
    assert md["guid"] == f"{ROOT_ID}://season/season-1000-official-1"
    assert md["parentRatingKey"] == "series-1000"
    assert md["parentKey"] == "/library/metadata/series-1000"
    assert md["parentGuid"] == f"{ROOT_ID}://show/series-1000"
    assert md["parentType"] == "show"
    assert md["parentTitle"] == "Galaxy Patrol"
    assert md["thumb"].endswith("seasons/5001.jpg")
    assert md["summary"] == "The first season."
    assert md["originallyAvailableAt"] == "2001-09-10"
    assert md["Guid"] == [{"id": "tvdb://5001"}]
    assert "Children" not in md


def test_season_include_children_and_names(client):
    md = one(client.get("/library/metadata/season-1000-dvd-2", params={"includeChildren": 1}))
    assert md["title"] == "Volume Two"
    assert [c["ratingKey"] for c in md["Children"]["Metadata"]] == [
        "episode-1000-dvd-9004", "episode-1000-dvd-9005", "episode-1000-dvd-9006",
    ]
    assert md["Children"]["Metadata"][0]["parentTitle"] == "Volume Two"


def test_season_titles_can_be_disabled(make_client):
    c = make_client({"defaults": {"season_titles": False}})
    assert one(c.get("/library/metadata/season-1000-dvd-2"))["title"] == "Season 2"


def test_season_children(client):
    eps = items(client.get("/library/metadata/season-1000-dvd-1/children"))
    assert [(e["ratingKey"], e["index"], e["parentIndex"]) for e in eps] == [
        ("episode-1000-dvd-9002", 1, 1), ("episode-1000-dvd-9001", 2, 1), ("episode-1000-dvd-9003", 3, 1),
    ]


def test_season_key_order_wins_over_url_profile(client):
    # A season stored with an aired key keeps aired numbering even via the DVD URL.
    eps = items(client.get("/profiles/dvd/library/metadata/season-1000-official-1/children"))
    assert eps[0]["ratingKey"] == "episode-1000-official-9001"


# ---------------------------------------------------------- episodes
def test_episode_metadata(client):
    md = one(client.get("/library/metadata/episode-1000-dvd-9001"))
    assert md["type"] == "episode"
    assert md["title"] == "Pilot"
    assert (md["parentIndex"], md["index"]) == (1, 2)
    assert md["key"] == "/library/metadata/episode-1000-dvd-9001"
    assert md["parentRatingKey"] == "season-1000-dvd-1"
    assert md["parentKey"] == "/library/metadata/season-1000-dvd-1"
    assert md["parentType"] == "season"
    assert md["parentTitle"] == "Season 1"
    assert md["grandparentRatingKey"] == "series-1000"
    assert md["grandparentGuid"] == f"{ROOT_ID}://show/series-1000"
    assert md["grandparentType"] == "show"
    assert md["grandparentTitle"] == "Galaxy Patrol"
    assert md["originallyAvailableAt"] == "2001-09-10"
    assert md["duration"] == 42 * 60_000
    assert md["Image"][0]["type"] == "snapshot"
    assert [g["id"] for g in md["Guid"]] == ["tvdb://9001", "imdb://tt809001"]
    assert [r["tag"] for r in md["Role"]][:3] == ["Gus Guest", "Ann Actor", "Bob Builder"]
    assert md["Role"][0]["role"] == "Villain"
    assert md["Director"][0]["tag"] == "Epi Director"
    assert md["Writer"][0]["tag"] == "Epi Writer"
    assert md["contentRating"] == "TV-PG"


def test_episode_not_in_order_is_404(client):
    assert client.get("/library/metadata/episode-1000-dvd-9100").status_code == 404


def test_bad_keys(client):
    assert client.get("/library/metadata/garbage").status_code == 404
    assert client.get("/library/metadata/season-1000-bogus-1").status_code == 404
    assert client.get("/library/metadata/series-99999").status_code == 404
    assert client.get("/library/metadata/season-1000-official-9").status_code == 404
    assert client.get("/library/metadata/series-1000/children", headers={"X-Plex-Container-Size": "x"}).status_code == 400


def test_episode_has_no_children(client):
    assert items(client.get("/library/metadata/episode-1000-official-9001/children")) == []
    assert items(client.get("/library/metadata/season-1000-official-1/grandchildren")) == []


# ---------------------------------------------------------- languages
def test_german_language(client):
    headers = {"X-Plex-Language": "de-DE", "X-Plex-Country": "DE"}
    md = one(client.get("/library/metadata/series-1000", headers=headers))
    assert md["title"] == "Galaxie-Patrouille"
    assert md["originalTitle"] == "Galaxy Patrol"
    assert md["summary"].startswith("Ein Patrouillenschiff")
    assert md["thumb"].endswith("1000-deu.jpg")
    assert md["contentRating"] == "de/12"
    assert any(i["url"].endswith("1000-eng.jpg") for i in md["OriginalImage"])
    eps = items(client.get("/library/metadata/series-1000/grandchildren", headers=headers))
    titles = {e["ratingKey"]: e["title"] for e in eps}
    assert titles["episode-1000-official-9001"] == "Pilotfolge"
    assert titles["episode-1000-official-9003"] == "Drift"  # no German title -> original


def test_language_as_query_param(client):
    md = one(client.get("/library/metadata/series-1000", params={"X-Plex-Language": "de"}))
    assert md["title"] == "Galaxie-Patrouille"


def test_forced_profile_language(make_client):
    c = make_client({"profiles": {"german": {"language": "de", "force_language": True}}})
    md = one(c.get("/profiles/german/library/metadata/series-1000", headers={"X-Plex-Language": "en-US"}))
    assert md["title"] == "Galaxie-Patrouille"


def test_translated_episode_titles(client):
    md = one(client.get("/library/metadata/series-3000", headers={"X-Plex-Language": "en-US"}))
    assert md["title"] == "Space Police"
    assert md["originalTitle"] == "Uchuu Keisatsu"
    assert md["summary"] == "Cops in space."
    eps = items(client.get("/library/metadata/series-3000/grandchildren", headers={"X-Plex-Language": "en-US"}))
    assert [e["title"] for e in eps] == ["The Beginning", "Tsuzuki"]
    assert eps[0]["summary"] == "It begins."


# -------------------------------------------------------------- images
def test_images_endpoint(client):
    container = client.get("/library/metadata/series-1000/images").json()["MediaContainer"]
    imgs = container["Image"]
    assert container["size"] == len(imgs) == 5
    assert [i["type"] for i in imgs] == ["coverPoster", "coverPoster", "background", "background", "clearLogo"]
    assert imgs[0]["url"].endswith("1000-eng.jpg")
    assert imgs[2]["url"].endswith("fanart/1000-1.jpg")

    season = client.get("/library/metadata/season-1000-official-1/images").json()["MediaContainer"]["Image"]
    urls = [i["url"] for i in season]
    assert any(u.endswith("seasons/5001.jpg") for u in urls) and any(u.endswith("5001-x.jpg") for u in urls)

    episode = client.get("/library/metadata/episode-1000-official-9001/images").json()["MediaContainer"]["Image"]
    assert episode == [{"type": "snapshot", "url": "https://artworks.thetvdb.com/banners/episodes/1000/9001.jpg", "alt": "Pilot"}]


# ------------------------------------------------------------ caching
def test_tvdb_responses_are_cached(client, fake):
    for _ in range(3):
        client.get("/library/metadata/series-1000")
        client.get("/library/metadata/series-1000/grandchildren")
    assert fake.count("/series/1000/extended") == 1
    assert fake.count("/series/1000/episodes/official?page=0") == 1
    assert fake.count("/artwork/types") == 1


def test_tvdb_relogin_on_401(client, fake):
    client.get("/library/metadata/series-1000")
    fake.expire_next = True
    assert client.get("/library/metadata/series-3000").status_code == 200
    assert fake.logins == 2


def test_bad_tvdb_key_returns_500(make_client, fake):
    from tvdb_agent.app import create_app
    from tvdb_agent.config import build_settings
    from tvdb_agent.tvdb import TVDBClient
    from fastapi.testclient import TestClient

    settings = build_settings({"tvdb": {"api_key": "wrong"}}, env=False)
    with TestClient(create_app(settings, tvdb=TVDBClient("wrong", transport=fake.transport))) as c:
        resp = c.get("/library/metadata/series-1000")
    assert resp.status_code == 500
    assert "TVDB authentication failed" in resp.json()["error"]


def test_bad_key_does_not_hammer_login(fake):
    import asyncio

    from tvdb_agent.tvdb import TVDBAuthError, TVDBClient

    async def run():
        client = TVDBClient("wrong", transport=fake.transport)
        for _ in range(3):
            try:
                await client.series_extended(1000)
            except TVDBAuthError:
                pass
        await client.aclose()

    calls = []
    original = fake.handle
    fake.handle = lambda req: (calls.append(req.url.path), original(req))[1]
    asyncio.run(run())
    assert calls.count("/v4/login") == 1


def test_unwritable_data_dir_gives_clear_error(make_client, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    c = make_client({"server": {"data_dir": str(blocker / "sub")}})
    resp = c.put("/admin/api/overrides/global/1000", json={"order": "dvd"})
    assert resp.status_code == 500
    assert "could not write" in resp.json()["error"]
    # The failed write did not leave a half-applied override behind.
    assert c.get("/admin/api/overrides").json()["overrides"] == []


def test_tvdb_404_maps_to_404(client, fake):
    original = fake.route

    def route(path, query):
        if path.startswith("/series/1000/episodes/"):
            import httpx
            return httpx.Response(404, json={"status": "failure"})
        return original(path, query)

    fake.route = route
    assert client.get("/library/metadata/series-1000/grandchildren").status_code == 404
