"""Async client for TheTVDB API v4 (https://api4.thetvdb.com/v4)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .cache import TTLCache

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api4.thetvdb.com/v4"
TOKEN_LIFETIME = 25 * 24 * 3600  # TVDB tokens are valid for one month.
MAX_PAGES = 100  # Safety stop when following episode pagination.


class TVDBError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class TVDBNotFound(TVDBError):
    pass


class TVDBAuthError(TVDBError):
    pass


class TVDBClient:
    def __init__(
        self,
        api_key: str,
        pin: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        cache: TTLCache | None = None,
        ttl: float = 12 * 3600,
        static_ttl: float = 7 * 24 * 3600,
        max_concurrency: int = 8,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str = "tvdb-custom-agent",
    ) -> None:
        self.api_key = api_key
        self.pin = pin or None
        self.base_url = base_url.rstrip("/")
        self.cache = cache or TTLCache()
        self.ttl = ttl
        self.static_ttl = static_ttl
        self._sem = asyncio.Semaphore(max_concurrency)
        self._token: str | None = None
        self._token_time = 0.0
        self._auth_error: TVDBAuthError | None = None
        self._auth_error_until = 0.0
        self._login_lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": user_agent},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ auth
    async def _login(self, force: bool = False) -> str:
        async with self._login_lock:
            fresh = self._token and time.monotonic() - self._token_time < TOKEN_LIFETIME
            if fresh and not force:
                return self._token  # type: ignore[return-value]
            if not self.api_key:
                raise TVDBAuthError("TVDB_API_KEY is not configured", 401)
            if self._auth_error is not None and time.monotonic() < self._auth_error_until:
                raise self._auth_error  # don't hammer /login with a bad key
            body: dict[str, str] = {"apikey": self.api_key}
            if self.pin:
                body["pin"] = self.pin
            resp = await self._http.post("/login", json=body)
            if resp.status_code != 200:
                error = TVDBAuthError(
                    f"TVDB login failed ({resp.status_code}): {resp.text[:200].strip()}", resp.status_code
                )
                if resp.status_code in (401, 403):
                    self._auth_error, self._auth_error_until = error, time.monotonic() + 60
                raise error
            self._auth_error = None
            token = (resp.json().get("data") or {}).get("token")
            if not token:
                raise TVDBAuthError("TVDB login returned no token", resp.status_code)
            self._token = token
            self._token_time = time.monotonic()
            log.info("Authenticated with TheTVDB")
            return token

    # --------------------------------------------------------------- request
    async def _request(self, path: str, params: dict | None = None) -> dict:
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        attempt = 0
        relogged = False
        while True:
            token = await self._login()
            async with self._sem:
                try:
                    resp = await self._http.get(
                        path, params=clean, headers={"Authorization": f"Bearer {token}"}
                    )
                except httpx.HTTPError as exc:
                    if attempt < 2:
                        attempt += 1
                        await asyncio.sleep(0.5 * attempt)
                        continue
                    raise TVDBError(f"TVDB request failed: {exc}") from exc
            if resp.status_code == 401 and not relogged:
                relogged = True
                await self._login(force=True)
                continue
            if resp.status_code == 404:
                raise TVDBNotFound(f"TVDB: {path} not found", 404)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                attempt += 1
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else attempt
                await asyncio.sleep(min(delay, 10))
                continue
            if resp.status_code >= 400:
                raise TVDBError(f"TVDB {path} -> {resp.status_code}: {resp.text[:200]}", resp.status_code)
            log.debug("TVDB GET %s %s", path, clean)
            return resp.json()

    async def _get(self, path: str, params: dict | None = None, *, ttl: float | None = None) -> Any:
        key = (path, tuple(sorted((params or {}).items())))
        ttl = self.ttl if ttl is None else ttl

        async def fetch() -> Any:
            return await self._request(path, params)

        return await self.cache.get_or_fetch(key, ttl, fetch)

    async def _data(self, path: str, params: dict | None = None, *, ttl: float | None = None) -> Any:
        payload = await self._get(path, params, ttl=ttl)
        return payload.get("data")

    # ------------------------------------------------------------- endpoints
    async def series_extended(self, series_id: int, language: str | None = None) -> dict:
        params: dict[str, Any] = {"meta": "translations"}
        if language:
            params["language"] = language
        data = await self._data(f"/series/{int(series_id)}/extended", params)
        if not data:
            raise TVDBNotFound(f"TVDB series {series_id} not found", 404)
        return data

    async def series_episodes(self, series_id: int, season_type: str, language: str | None = None) -> list[dict]:
        """All episodes of a series in one ordering (follows pagination).

        With ``language`` the translated listing endpoint is used; its
        records carry ``name``/``overview`` in that language when available.
        """
        path = f"/series/{int(series_id)}/episodes/{season_type}"
        if language:
            path += f"/{language}"

        key = ("episodes-all", path)

        async def fetch_all() -> list[dict]:
            episodes: list[dict] = []
            for page in range(MAX_PAGES):
                payload = await self._request(path, {"page": page})
                data = payload.get("data") or {}
                batch = data.get("episodes") or []
                episodes.extend(batch)
                links = payload.get("links") or {}
                if not batch or not links.get("next"):
                    break
            return episodes

        return await self.cache.get_or_fetch(key, self.ttl, fetch_all)

    async def episode(self, episode_id: int) -> dict:
        data = await self._data(f"/episodes/{int(episode_id)}")
        if not data:
            raise TVDBNotFound(f"TVDB episode {episode_id} not found", 404)
        return data

    async def episode_extended(self, episode_id: int) -> dict:
        data = await self._data(f"/episodes/{int(episode_id)}/extended", {"meta": "translations"})
        if not data:
            raise TVDBNotFound(f"TVDB episode {episode_id} not found", 404)
        return data

    async def season_extended(self, season_id: int) -> dict:
        data = await self._data(f"/seasons/{int(season_id)}/extended")
        if not data:
            raise TVDBNotFound(f"TVDB season {season_id} not found", 404)
        return data

    async def season_translation(self, season_id: int, language: str) -> dict | None:
        try:
            return await self._data(f"/seasons/{int(season_id)}/translations/{language}")
        except TVDBNotFound:
            return None

    async def search_series(self, query: str, year: int | None = None, limit: int = 20) -> list[dict]:
        params: dict[str, Any] = {"query": query, "type": "series", "limit": limit}
        if year:
            params["year"] = year
        return await self._data("/search", params) or []

    async def search_remote_id(self, remote_id: str) -> list[dict]:
        try:
            return await self._data(f"/search/remoteid/{remote_id}") or []
        except TVDBNotFound:
            return []

    async def artwork_types(self) -> list[dict]:
        return await self._data("/artwork/types", ttl=self.static_ttl) or []

    async def countries(self) -> list[dict]:
        return await self._data("/countries", ttl=self.static_ttl) or []
