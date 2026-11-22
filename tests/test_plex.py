"""Tests for registering the agent's orderings with Plex.

FakePlex copies the behaviour observed on a real Plex Media Server 1.43.5:
duplicate identifiers are refused with 409, an unreachable provider URL
with 400, and the token is sent as X-Plex-Token.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from tvdb_agent import __main__ as cli
from tvdb_agent.config import build_settings
from tvdb_agent.plex import PlexClient, agent_title, register, resolve_profiles

TOKEN = "plex-token"
ROOT_ID = "tv.plex.agents.custom.tvdbcustomagent"


class FakePlex:
    def __init__(self, reachable_prefix: str = "http://agent:8000") -> None:
        self.reachable_prefix = reachable_prefix
        self.providers: list[dict] = [
            {"id": 2, "identifier": "tv.plex.agents.series", "title": "Plex Series", "uri": "", "agentType": "primary"},
        ]
        self.groups: list[dict] = [
            {"id": 2, "title": "Plex Series", "primaryIdentifier": "tv.plex.agents.series", "builtIn": True},
        ]
        self.calls: list[str] = []
        self._next = 100

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.calls.append(f"{request.method} {url.path}")
        if request.headers.get("X-Plex-Token") != TOKEN:
            return httpx.Response(401, text="<html>Unauthorized</html>")
        path = url.path
        if path == "/media/providers/metadata" and request.method == "GET":
            return httpx.Response(200, json={"MediaContainer": {"MetadataAgentProvider": self.providers}})
        if path == "/media/providers/metadata" and request.method == "POST":
            uri = q["uri"]
            if not uri.startswith(self.reachable_prefix):
                return httpx.Response(400, text="An unknown network error occurred while trying to reach the provider URL.")
            ident = ROOT_ID + ("." + uri.rsplit("/profiles/", 1)[1] if "/profiles/" in uri else "")
            if any(p["identifier"] == ident for p in self.providers):
                return httpx.Response(409, text="A provider with the same identifier already exists.")
            self._next += 1
            prov = {"id": self._next, "identifier": ident, "title": "x", "uri": uri, "agentType": "primary"}
            self.providers.append(prov)
            return httpx.Response(200, json={"MediaContainer": {"MetadataAgentProvider": [prov]}})
        if path.startswith("/media/providers/metadata/group"):
            if request.method == "GET":
                return httpx.Response(200, json={"MediaContainer": {"MetadataAgentProviderGroup": self.groups}})
            if request.method == "POST":
                self._next += 1
                group = {"id": self._next, "title": q["title"], "primaryIdentifier": q["primaryIdentifier"]}
                self.groups.append(group)
                return httpx.Response(200, json={"MediaContainer": {"MetadataAgentProviderGroup": [group]}})
            if request.method == "PUT":
                gid = int(path.rsplit("/", 1)[1])
                next(g for g in self.groups if g["id"] == gid)["title"] = q["title"]
                return httpx.Response(200)
        if path.startswith("/media/providers/metadata/") and request.method == "PUT":
            pid = int(path.rsplit("/", 1)[1])
            next(p for p in self.providers if p["id"] == pid)["uri"] = q["uri"]
            return httpx.Response(200)
        return httpx.Response(404, text="Not Found")


def settings(**extra):
    data = {"tvdb": {"api_key": "k"}, "profiles": {"anime": {"episode_order": "absolute", "title": "TheTVDB Anime"}}}
    data.update(extra)
    return build_settings(data, env=False)


def run_register(fake, s, names=None, agent_url="http://agent:8000", token=TOKEN):
    async def go():
        client = PlexClient("http://plex:32400", token, transport=fake.transport)
        try:
            return await register(client, s, resolve_profiles(s, names), agent_url)
        finally:
            await client.aclose()

    return asyncio.run(go())


def test_default_profiles_and_titles():
    s = settings()
    names = [p.name or "" for p in resolve_profiles(s, None)]
    assert names == ["", "dvd", "absolute", "anime"]
    assert agent_title(s, s.root) == "TheTVDB (self-hosted) - TVDB default order"
    assert agent_title(s, s.profile("dvd")) == "TheTVDB (self-hosted) - DVD order"
    assert agent_title(s, s.profile("anime")) == "TheTVDB Anime"
    assert [p.name for p in resolve_profiles(s, ["root", "regional"])] == [None, "regional"]
    with pytest.raises(ValueError):
        resolve_profiles(s, ["nope"])


def test_register_creates_then_is_idempotent():
    fake, s = FakePlex(), settings()
    first = run_register(fake, s)
    assert [(r.profile, r.provider, r.group, r.error) for r in first] == [
        ("", "created", "created", None),
        ("dvd", "created", "created", None),
        ("absolute", "created", "created", None),
        ("anime", "created", "created", None),
    ]
    assert first[1].uri == "http://agent:8000/profiles/dvd"
    titles = {g["primaryIdentifier"]: g["title"] for g in fake.groups}
    assert titles[ROOT_ID + ".dvd"] == "TheTVDB (self-hosted) - DVD order"
    assert titles["tv.plex.agents.series"] == "Plex Series"  # untouched

    second = run_register(fake, s)
    assert {(r.provider, r.group) for r in second} == {("unchanged", "unchanged")}
    assert len(fake.groups) == 5


def test_register_updates_uri_and_renames_agent():
    fake, s = FakePlex(reachable_prefix="http://"), settings()
    run_register(fake, s, ["root"], agent_url="http://old-host:8000")
    renamed = settings(provider={"title": "TVDB"})
    [result] = run_register(fake, renamed, ["root"], agent_url="http://agent:8000/")
    assert (result.provider, result.group) == ("updated", "renamed")
    assert fake.providers[-1]["uri"] == "http://agent:8000"
    assert fake.groups[-1]["title"] == "TVDB - TVDB default order"


def test_register_reports_unreachable_agent_per_profile():
    fake = FakePlex(reachable_prefix="http://agent:8000")
    [result] = run_register(fake, settings(), ["dvd"], agent_url="http://wrong:1")
    assert result.error and "unknown network error" in result.error
    assert result.provider == "" and fake.groups[-1]["primaryIdentifier"] == "tv.plex.agents.series"


def test_register_bad_token_and_bad_agent_url():
    from tvdb_agent.plex import PlexError

    with pytest.raises(PlexError, match="token"):
        run_register(FakePlex(), settings(), token="wrong")
    with pytest.raises(ValueError):
        run_register(FakePlex(), settings(), agent_url="agent:8000")


def test_admin_register_endpoint(fake, tmp_path):
    from fastapi.testclient import TestClient

    from fake_tvdb import FakeTVDB
    from tvdb_agent.app import create_app
    from tvdb_agent.tvdb import TVDBClient

    plex = FakePlex()
    s = build_settings({"tvdb": {"api_key": "test-key"}, "server": {"data_dir": str(tmp_path), "admin_token": "adm"},
                        "plex": {"url": "http://plex:32400", "token": TOKEN}}, env=False)
    app = create_app(s, tvdb=TVDBClient("test-key", transport=FakeTVDB().transport), plex_transport=plex.transport)
    hdr = {"X-Admin-Token": "adm"}
    with TestClient(app) as c:
        info = c.get("/admin/api/plex", headers=hdr).json()
        assert info["plexUrl"] == "http://plex:32400" and info["tokenConfigured"] is True
        assert [p["name"] for p in info["profiles"] if p["default"]] == ["", "dvd", "absolute"]
        assert c.post("/admin/api/plex/register", json={}).status_code == 401
        resp = c.post("/admin/api/plex/register", headers=hdr, json={"agentUrl": "http://agent:8000", "profiles": ["", "dvd"]})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert [(r["agent"], r["provider"]) for r in body["results"]] == [
            ("TheTVDB (self-hosted) - TVDB default order", "created"),
            ("TheTVDB (self-hosted) - DVD order", "created"),
        ]
        assert c.post("/admin/api/plex/register", headers=hdr, json={"profiles": ["dvd"]}).status_code == 400  # no agent URL
        bad = c.post("/admin/api/plex/register", headers=hdr, json={"agentUrl": "http://agent:8000", "token": "nope"})
        assert bad.status_code == 502 and "token" in bad.json()["detail"]
        assert c.post("/admin/api/plex/register", headers=hdr,
                      json={"agentUrl": "http://agent:8000", "profiles": ["missing"]}).status_code == 400


def test_cli_register_plex(monkeypatch, capsys):
    plex = FakePlex()
    real_init = PlexClient.__init__

    def init(self, base_url, token=None, **kw):
        real_init(self, base_url, token, transport=plex.transport)

    monkeypatch.setattr(PlexClient, "__init__", init)
    monkeypatch.setenv("TVDB_API_KEY", "k")
    monkeypatch.delenv("CONFIG_FILE", raising=False)
    code = cli.main(["register-plex", "--plex-url", "http://plex:32400", "--token", TOKEN,
                     "--agent-url", "http://agent:8000", "--profile", "root", "--profile", "absolute"])
    out = capsys.readouterr().out
    assert code == 0
    assert "TheTVDB (self-hosted) - Absolute order" in out and "Refresh All Metadata" in out
    assert [g["title"] for g in plex.groups][-2:] == [
        "TheTVDB (self-hosted) - TVDB default order", "TheTVDB (self-hosted) - Absolute order"]
    assert cli.main(["register-plex", "--token", TOKEN, "--agent-url", "http://agent:8000"]) == 2  # no Plex URL
