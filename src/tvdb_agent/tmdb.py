"""Optional TheMovieDB lookups: TMDB ids, IMDb ids TVDB lacks, TMDB ratings.

TMDB can look a show or an episode up by its TVDB id
(``/find/{id}?external_source=tvdb_id``), so it works whatever episode order
the library uses. Enabled when ``TMDB_API_KEY`` is set; both a v4 "API Read
Access Token" (sent as a Bearer header) and a v3 "API Key" (sent as the
``api_key`` parameter) work.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .cache import TTLCache

log = logging.getLogger(__name__)

BASE_URL = "https://api.themoviedb.org/3"


class _Unavailable(Exception):
    """A failed lookup; raised so the cache does not keep it."""


class TMDBClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        ttl: float = 3 * 24 * 3600,
        max_concurrency: int = 8,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.api_key = api_key.strip()
        self._bearer = self.api_key.startswith("eyJ")  # v4 tokens are JWTs
        headers = {"Accept": "application/json", "User-Agent": "tvdb-custom-agent"}
        if self._bearer:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout,
                                       transport=transport)
        self.cache = TTLCache(8192)
        self.ttl = ttl
        self._sem = asyncio.Semaphore(max_concurrency)
        self._disabled_until = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict | None = None) -> dict | None:
        key = (path, tuple(sorted((params or {}).items())))

        async def fetch() -> dict:
            if time.monotonic() < self._disabled_until:
                raise _Unavailable("disabled after a 401")
            query = dict(params or {})
            if not self._bearer:
                query["api_key"] = self.api_key
            for attempt in range(3):
                async with self._sem:
                    try:
                        resp = await self._http.get(path, params=query)
                    except httpx.HTTPError as exc:
                        raise _Unavailable(f"TMDB {path} failed: {exc}") from exc
                if resp.status_code == 429 and attempt < 2:
                    retry = resp.headers.get("Retry-After", "1")
                    await asyncio.sleep(min(float(retry) if retry.replace(".", "").isdigit() else 1.0, 10.0))
                    continue
                if resp.status_code == 401:
                    # Bad key: say so once and stop asking for 10 minutes.
                    self._disabled_until = time.monotonic() + 600
                    log.error("TMDB rejected TMDB_API_KEY (401): %s", resp.text[:200])
                    raise _Unavailable("401")
                if resp.status_code == 404:
                    return {}
                if resp.status_code >= 400:
                    raise _Unavailable(f"TMDB {path} -> {resp.status_code}")
                return resp.json()
            raise _Unavailable(f"TMDB {path}: rate limited")

        try:
            return await self.cache.get_or_fetch(key, self.ttl, fetch)
        except _Unavailable as exc:
            log.debug("%s", exc)
            return None

    async def find_by_tvdb(self, tvdb_id: int) -> dict:
        return await self._get(f"/find/{int(tvdb_id)}", {"external_source": "tvdb_id"}) or {}

    async def tv(self, tmdb_id: int | str) -> dict:
        return await self._get(f"/tv/{int(tmdb_id)}", {"append_to_response": "external_ids"}) or {}

    async def episode_external_ids(self, show_id: int, season: int, episode: int) -> dict:
        path = f"/tv/{int(show_id)}/season/{int(season)}/episode/{int(episode)}/external_ids"
        return await self._get(path) or {}


def tmdb_rating(record: dict[str, Any] | None) -> float | None:
    """TMDB's 0-10 ``vote_average``, ignoring titles nobody has voted on."""
    if not record:
        return None
    votes = record.get("vote_count")
    value = record.get("vote_average")
    if not value or (votes is not None and votes <= 0):
        return None
    return round(float(value), 1)
