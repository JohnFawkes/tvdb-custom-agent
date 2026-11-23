from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from fake_tvdb import FakeTVDB
from tvdb_agent.app import create_app
from tvdb_agent.config import build_settings
from tvdb_agent.tvdb import TVDBClient


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@pytest.fixture
def fake() -> FakeTVDB:
    return FakeTVDB()


@pytest.fixture
def make_client(fake, tmp_path):
    clients = []

    def _make(config: dict | None = None, app_kwargs: dict | None = None) -> TestClient:
        data = _merge({"tvdb": {"api_key": "test-key"}, "server": {"data_dir": str(tmp_path)},
                       "imdb": {"ratings": False}}, config or {})
        settings = build_settings(data, env=False)
        tvdb = TVDBClient("test-key", transport=fake.transport)
        client = TestClient(create_app(settings, tvdb=tvdb, **(app_kwargs or {})))
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


def items(resp) -> list[dict]:
    assert resp.status_code == 200, resp.text
    return resp.json()["MediaContainer"].get("Metadata", [])


def one(resp) -> dict:
    found = items(resp)
    assert len(found) == 1, found
    return found[0]
