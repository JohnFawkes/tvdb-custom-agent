"""Tests for POST /library/metadata/matches."""

from __future__ import annotations

from conftest import items, one

ROOT_ID = "tv.plex.agents.custom.tvdbcustomagent"
MATCH = "/library/metadata/matches"


def test_match_show_by_title_and_year(client):
    md = one(client.post(MATCH, json={"type": 2, "title": "Galaxy Patrol", "year": 2001}))
    assert md["ratingKey"] == "series-1000"
    assert md["guid"] == f"{ROOT_ID}://show/series-1000"
    assert md["score"] >= 90
    assert md["Genre"]  # best match carries full metadata


def test_match_show_without_year_prefers_exact_title(client):
    md = one(client.post(MATCH, json={"type": 2, "title": "Galaxy Patrol"}))
    assert md["ratingKey"] == "series-1000"


def test_match_show_title_with_year_suffix(client):
    md = one(client.post(MATCH, json={"type": 2, "title": "Galaxy Patrol (2001)"}))
    assert md["ratingKey"] == "series-1000"


def test_match_manual_returns_ranked_list(client):
    found = items(client.post(MATCH, json={"type": 2, "title": "Galaxy Patrol", "manual": 1}))
    assert [m["ratingKey"] for m in found] == ["series-1000", "series-2000"]
    assert found[0]["score"] > found[1]["score"]
    assert found[1]["year"] == 2010
    assert found[1]["guid"] == f"{ROOT_ID}://show/series-2000"


def test_match_include_children(client):
    md = one(client.post(MATCH, json={"type": 2, "title": "Galaxy Patrol", "year": 2001, "includeChildren": 1}))
    assert [c["index"] for c in md["Children"]["Metadata"]] == [0, 1, 2]


def test_match_include_children_with_episode_order(client):
    md = one(client.post(MATCH, json={"type": 2, "title": "Galaxy Patrol", "includeChildren": 1, "episodeOrder": "tvdbAbsolute"}))
    assert [c["ratingKey"] for c in md["Children"]["Metadata"]] == ["season-1000-absolute-1"]


def test_match_tvdb_tag_in_filename_skips_search(client, fake):
    body = {"type": 2, "title": "Something Else", "filename": "TV Shows/Galaxy Patrol (2001) {tvdb-1000}/S01E01.mkv"}
    md = one(client.post(MATCH, json=body))
    assert md["ratingKey"] == "series-1000"
    assert md["score"] == 100
    assert fake.count("/search") == 0


def test_match_by_tvdb_guid(client):
    assert one(client.post(MATCH, json={"type": 2, "title": "x", "guid": "tvdb://1000"}))["ratingKey"] == "series-1000"


def test_match_by_imdb_guid(client):
    assert one(client.post(MATCH, json={"type": 2, "title": "x", "guid": "imdb://tt7000001"}))["ratingKey"] == "series-1000"


def test_match_by_own_guid_from_another_profile(client):
    guid = f"{ROOT_ID}.dvd://show/series-1000"
    md = one(client.post(MATCH, json={"type": 2, "title": "x", "guid": guid}))
    assert md["guid"] == f"{ROOT_ID}://show/series-1000"


def test_match_unknown_id_falls_back_to_search(client):
    md = one(client.post(MATCH, json={"type": 2, "title": "Galaxy Patrol", "guid": "tvdb://424242"}))
    assert md["ratingKey"] == "series-1000"


def test_match_no_result(client):
    resp = client.post(MATCH, json={"type": 2, "title": "Nothing Like This"})
    container = resp.json()["MediaContainer"]
    assert container["size"] == 0 and container["Metadata"] == []


def test_match_low_confidence_returns_nothing(make_client):
    c = make_client({"defaults": {"match_min_score": 99}})
    assert items(c.post(MATCH, json={"type": 2, "title": "Galaxy Patrl", "year": 1990})) == []


def test_match_season(client):
    md = one(client.post(MATCH, json={"type": 3, "parentTitle": "Galaxy Patrol", "index": 2, "includeChildren": 1}))
    assert md["ratingKey"] == "season-1000-official-2"
    assert md["parentTitle"] == "Galaxy Patrol"
    assert len(md["Children"]["Metadata"]) == 2


def test_match_season_through_dvd_profile(client):
    md = one(client.post("/profiles/dvd" + MATCH, json={"type": 3, "parentTitle": "Galaxy Patrol", "index": 2}))
    assert md["ratingKey"] == "season-1000-dvd-2"
    assert md["guid"].startswith(f"{ROOT_ID}.dvd://season/")


def test_match_episode_by_index(client):
    md = one(client.post(MATCH, json={"type": 4, "grandparentTitle": "Galaxy Patrol", "parentIndex": 1, "index": 2}))
    assert md["ratingKey"] == "episode-1000-official-9002"
    assert md["title"] == "First Contact"


def test_match_episode_with_episode_order_in_body(client):
    body = {"type": 4, "grandparentTitle": "Galaxy Patrol", "parentIndex": "1", "index": "2", "episodeOrder": "tvdbDvd"}
    assert one(client.post(MATCH, json=body))["ratingKey"] == "episode-1000-dvd-9001"


def test_match_episode_by_date(client):
    md = one(client.post(MATCH, json={"type": 4, "grandparentTitle": "Galaxy Patrol", "date": "2002-09-16"}))
    assert md["ratingKey"] == "episode-1000-official-9006"


def test_match_episode_by_tvdb_episode_guid(client):
    md = one(client.post(MATCH, json={"type": 4, "grandparentTitle": "?", "guid": "tvdb://9004"}))
    assert md["ratingKey"] == "episode-1000-official-9004"


def test_match_episode_with_folder_tag(client, fake):
    body = {"type": 4, "grandparentTitle": "GP", "parentIndex": 2, "index": 1,
            "filename": "TV/Galaxy Patrol {tvdb-1000}/Season 02/GP S02E01.mkv"}
    assert one(client.post(MATCH, json=body))["ratingKey"] == "episode-1000-official-9005"
    assert fake.count("/search") == 0


def test_match_episode_not_found(client):
    assert items(client.post(MATCH, json={"type": 4, "grandparentTitle": "Galaxy Patrol", "parentIndex": 9, "index": 9})) == []


def test_match_bad_requests(client):
    assert client.post(MATCH, json={"type": 1, "title": "A Movie"}).status_code == 400
    assert client.post(MATCH, json={"title": "No type"}).status_code == 400
    assert client.post(MATCH, json=[1, 2]).status_code == 400


def test_match_response_customization(client):
    body = {
        "type": 2, "title": "Galaxy Patrol", "year": 2001, "includeChildren": 1,
        "includeElements": "Metadata,Children", "includeFields": "title,index,year",
    }
    md = one(client.post(MATCH, json=body))
    assert set(md) == {"ratingKey", "key", "guid", "type", "title", "year", "Children"}
    child = md["Children"]["Metadata"][1]
    assert set(child) == {"ratingKey", "key", "guid", "type", "title", "index", "year"}


def test_match_form_encoded_body(client):
    resp = client.post(MATCH, content="type=2&title=Galaxy+Patrol&year=2001",
                       headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert one(resp)["ratingKey"] == "series-1000"


def test_match_language_header(client):
    md = one(client.post(MATCH + "?X-Plex-Language=de-DE", json={"type": 2, "title": "Galaxy Patrol", "year": 2001}))
    assert md["title"] == "Galaxie-Patrouille"
