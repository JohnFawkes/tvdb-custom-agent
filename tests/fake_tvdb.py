"""An in-process fake of the TVDB v4 API used by the tests.

All shows here are fictional; record shapes follow TVDB's swagger spec.
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

import httpx

PAGE_SIZE = 4  # small, so pagination is exercised

ARTWORK_TYPES = [
    {"id": 1, "name": "Banner", "recordType": "series", "slug": "banners"},
    {"id": 2, "name": "Poster", "recordType": "series", "slug": "posters"},
    {"id": 3, "name": "Background", "recordType": "series", "slug": "backgrounds"},
    {"id": 7, "name": "Poster", "recordType": "season", "slug": "posters"},
    {"id": 8, "name": "Background", "recordType": "season", "slug": "backgrounds"},
    {"id": 11, "name": "16:9 Screencap", "recordType": "episode", "slug": "screencap"},
    {"id": 23, "name": "ClearLogo", "recordType": "series", "slug": "clearlogo"},
]

COUNTRIES = [
    {"id": "usa", "shortCode": "us", "name": "United States of America"},
    {"id": "deu", "shortCode": "de", "name": "Germany"},
    {"id": "jpn", "shortCode": "jp", "name": "Japan"},
]

ST = {
    "official": {"id": 1, "name": "Aired Order", "type": "official", "alternateName": None},
    "dvd": {"id": 2, "name": "DVD Order", "type": "dvd", "alternateName": None},
    "absolute": {"id": 3, "name": "Absolute Order", "type": "absolute", "alternateName": None},
    "alternate": {"id": 4, "name": "Alternate Order", "type": "alternate", "alternateName": "Netflix"},
}


def _ep(eid, season, number, name, aired, absolute=None, overview=None, runtime=42):
    return {
        "id": eid,
        "seriesId": 1000,
        "name": name,
        "aired": aired,
        "runtime": runtime,
        "seasonNumber": season,
        "number": number,
        "absoluteNumber": absolute,
        "overview": overview or f"Overview of {name}.",
        "image": f"https://artworks.thetvdb.com/banners/episodes/1000/{eid}.jpg",
    }


GP_EPISODES = {
    9001: ("Pilot", "2001-09-10", 1),
    9002: ("First Contact", "2001-09-17", 2),
    9003: ("Drift", "2001-09-24", 3),
    9004: ("Mutiny", "2001-10-01", 4),
    9005: ("Return", "2002-09-09", 5),
    9006: ("Finale", "2002-09-16", 6),
    9100: ("Behind the Scenes", "2001-12-24", None),
}

# episode id -> (season, number) per order
GP_ORDERS = {
    "official": {9001: (1, 1), 9002: (1, 2), 9003: (1, 3), 9004: (1, 4), 9005: (2, 1), 9006: (2, 2), 9100: (0, 1)},
    "dvd": {9002: (1, 1), 9001: (1, 2), 9003: (1, 3), 9004: (2, 1), 9005: (2, 2), 9006: (2, 3)},
    "absolute": {9001: (1, 1), 9002: (1, 2), 9003: (1, 3), 9004: (1, 4), 9005: (1, 5), 9006: (1, 6)},
}

GERMAN_NAMES = {9001: "Pilotfolge", 9002: "Erstkontakt"}

GALAXY_PATROL = {
    "id": 1000,
    "name": "Galaxy Patrol",
    "slug": "galaxy-patrol",
    "image": "https://artworks.thetvdb.com/banners/posters/1000-1.jpg",
    "firstAired": "2001-09-10",
    "year": "2001",
    "overview": "A patrol ship keeps the peace.",
    "originalCountry": "usa",
    "originalLanguage": "eng",
    "defaultSeasonType": 1,
    "averageRuntime": 42,
    "status": {"id": 2, "name": "Ended"},
    "genres": [{"id": 1, "name": "Science Fiction"}, {"id": 2, "name": "Drama"}],
    "companies": [
        {"id": 1, "name": "Nova Studios", "companyType": {"companyTypeId": 3, "companyTypeName": "Production Company"}},
        {"id": 2, "name": "SpaceNet", "companyType": {"companyTypeId": 1, "companyTypeName": "Network"}},
    ],
    "originalNetwork": {"id": 2, "name": "SpaceNet"},
    "latestNetwork": {"id": 2, "name": "SpaceNet"},
    "contentRatings": [
        {"id": 1, "name": "TV-PG", "country": "usa", "contentType": ""},
        {"id": 2, "name": "12", "country": "deu", "contentType": ""},
    ],
    "remoteIds": [
        {"id": "tt7000001", "type": 2, "sourceName": "IMDB"},
        {"id": "70001", "type": 12, "sourceName": "TheMovieDB.com"},
        {"id": "https://example.org", "type": 4, "sourceName": "Official Website"},
    ],
    "characters": [
        {"id": 1, "name": "Captain Nova", "peopleType": "Actor", "type": 3, "personName": "Ann Actor",
         "personImgURL": "https://artworks.thetvdb.com/banners/person/1.jpg", "sort": 1, "isFeatured": True},
        {"id": 2, "name": "Lt. Comet", "peopleType": "Actor", "type": 3, "personName": "Bob Builder", "sort": 2},
        {"id": 3, "name": "", "peopleType": "Director", "type": 1, "personName": "Dee Rector"},
        {"id": 4, "name": "", "peopleType": "Creator", "type": 6, "personName": "Cree Ator"},
        {"id": 5, "name": "", "peopleType": "Executive Producer", "type": 11, "personName": "Pro Ducer"},
    ],
    "artworks": [
        {"id": 11, "type": 2, "language": "eng", "score": 10, "image": "https://artworks.thetvdb.com/banners/posters/1000-eng.jpg"},
        {"id": 12, "type": 2, "language": "deu", "score": 5, "image": "https://artworks.thetvdb.com/banners/posters/1000-deu.jpg"},
        {"id": 13, "type": 3, "language": None, "score": 7, "image": "https://artworks.thetvdb.com/banners/fanart/1000-1.jpg"},
        {"id": 14, "type": 3, "language": "eng", "score": 9, "image": "https://artworks.thetvdb.com/banners/fanart/1000-2.jpg"},
        {"id": 15, "type": 23, "language": "eng", "score": 3, "image": "https://artworks.thetvdb.com/banners/clearlogo/1000.png"},
        {"id": 16, "type": 1, "language": "eng", "score": 3, "image": "https://artworks.thetvdb.com/banners/graphical/1000.jpg"},
        {"id": 17, "type": 7, "language": "eng", "score": 4, "seasonId": 5001, "image": "https://artworks.thetvdb.com/banners/seasons/5001.jpg"},
    ],
    "seasons": [
        {"id": 5000, "number": 0, "seriesId": 1000, "type": ST["official"], "name": None},
        {"id": 5001, "number": 1, "seriesId": 1000, "type": ST["official"], "image": "https://artworks.thetvdb.com/banners/seasons/5001-base.jpg"},
        {"id": 5002, "number": 2, "seriesId": 1000, "type": ST["official"]},
        {"id": 5101, "number": 1, "seriesId": 1000, "type": ST["dvd"]},
        {"id": 5102, "number": 2, "seriesId": 1000, "type": ST["dvd"], "name": "Volume Two"},
        {"id": 5201, "number": 1, "seriesId": 1000, "type": ST["absolute"]},
    ],
    "seasonTypes": [ST["official"], ST["dvd"], ST["absolute"], ST["alternate"]],
    "translations": {
        "nameTranslations": [
            {"language": "eng", "name": "Galaxy Patrol", "isPrimary": True},
            {"language": "deu", "name": "Galaxie-Patrouille"},
            {"language": "deu", "name": "GP", "isAlias": True},
        ],
        "overviewTranslations": [
            {"language": "eng", "overview": "A patrol ship keeps the peace."},
            {"language": "deu", "overview": "Ein Patrouillenschiff wahrt den Frieden."},
        ],
    },
}

# An anime-like show whose base records are Japanese.
SPACE_POLICE = {
    "id": 3000,
    "name": "Uchuu Keisatsu",
    "firstAired": "2015-04-01",
    "year": "2015",
    "overview": "Japanese overview.",
    "originalCountry": "jpn",
    "originalLanguage": "jpn",
    "defaultSeasonType": 1,
    "seasons": [
        {"id": 7001, "number": 1, "seriesId": 3000, "type": ST["official"]},
        {"id": 7101, "number": 1, "seriesId": 3000, "type": ST["absolute"]},
    ],
    "translations": {
        "nameTranslations": [
            {"language": "jpn", "name": "Uchuu Keisatsu"},
            {"language": "eng", "name": "Space Police"},
        ],
        "overviewTranslations": [{"language": "eng", "overview": "Cops in space."}],
    },
    "artworks": [],
    "characters": [],
}
SP_EPISODES_BASE = [
    {"id": 8001, "seriesId": 3000, "name": "Hajimari", "seasonNumber": 1, "number": 1, "aired": "2015-04-01", "overview": "JP"},
    {"id": 8002, "seriesId": 3000, "name": "Tsuzuki", "seasonNumber": 1, "number": 2, "aired": "2015-04-08", "overview": "JP"},
]
SP_EPISODES_ENG = [
    {"id": 8001, "name": "The Beginning", "seasonNumber": 1, "number": 1, "overview": "It begins."},
    {"id": 8002, "name": None, "seasonNumber": 1, "number": 2, "overview": None},
]

SEARCH_RESULTS = {
    "galaxy patrol": [
        {"objectID": "series-2000", "id": "series-2000", "tvdb_id": "2000", "name": "Galaxy Patrol: The Next Wave",
         "year": "2010", "type": "series", "image_url": "https://artworks.thetvdb.com/banners/posters/2000.jpg"},
        {"objectID": "series-1000", "id": "series-1000", "tvdb_id": "1000", "name": "Galaxy Patrol", "year": "2001",
         "type": "series", "first_air_time": "2001-09-10", "aliases": ["GP"],
         "translations": {"eng": "Galaxy Patrol", "deu": "Galaxie-Patrouille"},
         "overviews": {"eng": "A patrol ship keeps the peace."},
         "image_url": "https://artworks.thetvdb.com/banners/posters/1000-1.jpg"},
    ],
    "space police": [
        {"id": "series-3000", "tvdb_id": "3000", "name": "Uchuu Keisatsu", "year": "2015", "type": "series",
         "translations": {"eng": "Space Police"}},
    ],
}


class FakeTVDB:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.logins = 0
        self.token = "token-1"
        self.expire_next = False

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def count(self, prefix: str) -> int:
        return sum(1 for c in self.calls if c.startswith(prefix))

    # ------------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        path = url.path.removeprefix("/v4")
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if request.method == "POST" and path == "/login":
            body = json.loads(request.content or b"{}")
            if body.get("apikey") != "test-key":
                return httpx.Response(401, json={"status": "failure", "message": "invalid key"})
            self.logins += 1
            self.token = f"token-{self.logins}"
            return httpx.Response(200, json={"status": "success", "data": {"token": self.token}})
        if request.headers.get("Authorization") != f"Bearer {self.token}" or self.expire_next:
            self.expire_next = False
            self.token = "expired"
            return httpx.Response(401, json={"status": "failure", "message": "Unauthorized"})
        self.calls.append(path + (f"?{url.query}" if url.query else ""))
        return self.route(path, query)

    def ok(self, data, links=None) -> httpx.Response:
        body = {"status": "success", "data": data}
        if links is not None:
            body["links"] = links
        return httpx.Response(200, json=body)

    def route(self, path: str, query: dict) -> httpx.Response:
        if path == "/artwork/types":
            return self.ok(ARTWORK_TYPES)
        if path == "/countries":
            return self.ok(COUNTRIES)
        m = re.fullmatch(r"/series/(\d+)/extended", path)
        if m:
            sid = int(m.group(1))
            rec = {1000: GALAXY_PATROL, 3000: SPACE_POLICE}.get(sid)
            return self.ok(rec) if rec else httpx.Response(404, json={"status": "failure"})
        m = re.fullmatch(r"/series/(\d+)/episodes/([a-z]+)(?:/([a-z]+))?", path)
        if m:
            return self.episodes(int(m.group(1)), m.group(2), m.group(3), int(query.get("page", 0)))
        m = re.fullmatch(r"/episodes/(\d+)(/extended)?", path)
        if m:
            eid = int(m.group(1))
            if eid in GP_EPISODES:
                season, number = GP_ORDERS["official"][eid]
                name, aired, absolute = GP_EPISODES[eid]
                rec = _ep(eid, season, number, name, aired, absolute)
                if m.group(2):
                    rec |= {
                        "remoteIds": [{"id": f"tt80{eid}", "type": 2, "sourceName": "IMDB"}],
                        "characters": [
                            {"id": 50, "name": "Villain", "peopleType": "Guest Star", "personName": "Gus Guest"},
                            {"id": 51, "peopleType": "Director", "personName": "Epi Director"},
                            {"id": 52, "peopleType": "Writer", "personName": "Epi Writer"},
                        ],
                        "translations": {"nameTranslations": [{"language": "deu", "name": GERMAN_NAMES.get(eid, "")}]},
                    }
                return self.ok(rec)
            if eid in (8001, 8002):
                return self.ok(next(e for e in SP_EPISODES_BASE if e["id"] == eid))
            return httpx.Response(404, json={"status": "failure"})
        m = re.fullmatch(r"/seasons/(\d+)/extended", path)
        if m:
            sid = int(m.group(1))
            for show in (GALAXY_PATROL, SPACE_POLICE):
                for season in show["seasons"]:
                    if season["id"] == sid:
                        art = [{"id": 900, "type": 7, "language": "eng", "score": 1,
                                "image": f"https://artworks.thetvdb.com/banners/seasons/{sid}-x.jpg"}]
                        return self.ok(season | {"artwork": art})
            return httpx.Response(404, json={"status": "failure"})
        m = re.fullmatch(r"/seasons/(\d+)/translations/([a-z]+)", path)
        if m:
            if m.group(1) == "5001" and m.group(2) == "eng":
                return self.ok({"language": "eng", "name": "Season 1", "overview": "The first season."})
            return httpx.Response(404, json={"status": "failure"})
        if path == "/search":
            q = query.get("query", "").lower()
            rows = SEARCH_RESULTS.get(q, [])
            if query.get("year"):
                rows = [r for r in rows if r.get("year") == query["year"]]
            return self.ok(rows)
        m = re.fullmatch(r"/search/remoteid/(.+)", path)
        if m:
            rid = m.group(1)
            if rid == "tt7000001":
                return self.ok([{"series": {"id": 1000, "name": "Galaxy Patrol"}}])
            if rid == "tt809003":
                return self.ok([{"episode": {"id": 9003, "seriesId": 1000}}])
            return self.ok([])
        return httpx.Response(404, json={"status": "failure", "message": f"no route {path}"})

    def episodes(self, sid: int, season_type: str, lang: str | None, page: int) -> httpx.Response:
        if sid == 1000:
            order = GP_ORDERS.get(season_type, {})
            rows = []
            for eid, (season, number) in order.items():
                name, aired, absolute = GP_EPISODES[eid]
                if lang == "deu":
                    name = GERMAN_NAMES.get(eid)
                rows.append(_ep(eid, season, number, name, aired, absolute))
            series = {"id": 1000, "name": GALAXY_PATROL["name"]}
        elif sid == 3000:
            if season_type not in ("official", "absolute"):
                rows = []
            else:
                rows = [dict(e) for e in (SP_EPISODES_ENG if lang == "eng" else SP_EPISODES_BASE)] if lang in (None, "eng") else []
            series = {"id": 3000, "name": SPACE_POLICE["name"]}
        else:
            return httpx.Response(404, json={"status": "failure"})
        chunk = rows[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
        has_next = (page + 1) * PAGE_SIZE < len(rows)
        links = {"prev": None, "self": f"page={page}", "next": f"page={page + 1}" if has_next else None,
                 "total_items": len(rows), "page_size": PAGE_SIZE}
        return self.ok({"series": series, "episodes": chunk}, links)
