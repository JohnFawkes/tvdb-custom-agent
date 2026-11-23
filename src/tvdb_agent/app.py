"""FastAPI application implementing the Plex custom metadata provider API."""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import asynccontextmanager
from importlib import resources
from typing import Any
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import __version__, keys, orders
from .cache import TTLCache
from .config import GLOBAL_SCOPE_NAMES, Profile, Settings, load_settings
from .customize import Customization
from .overrides import GLOBAL, OverrideStore, OverrideStoreError
from .imdb import IMDbRatings
from .plex import PlexClient, PlexError, agent_title, default_profile_names, register, resolve_profiles
from .tmdb import TMDBClient
from .service import Agent, BadRequest, Ctx, NotFound
from .tvdb import TVDBAuthError, TVDBClient, TVDBError, TVDBNotFound

log = logging.getLogger("tvdb_agent")

DEFAULT_CHILD_PAGE = 20  # Plex: no paging headers -> first 20 children
_REPEATED_PREFIX = re.compile(r"^(/profiles/[A-Za-z0-9]+)(?:\1)+(?=/|$)")


class ProfilePrefixMiddleware:
    """Collapse ``/profiles/x/profiles/x/...`` into ``/profiles/x/...``.

    Version 0.1.0 advertised feature keys that already contained the profile
    prefix; Plex appends feature keys to the provider URL, so providers it
    registered back then request doubled paths until they are refreshed."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            fixed = _REPEATED_PREFIX.sub(r"\1", path)
            if fixed != path:
                scope = dict(scope)
                scope["path"] = fixed
                scope["raw_path"] = fixed.encode()
        await self.app(scope, receive, send)


def _param(request: Request, name: str) -> str | None:
    """X-Plex-* values may arrive as headers or query parameters."""
    value = request.query_params.get(name)
    if value in (None, ""):
        value = request.headers.get(name)
    return value if value not in (None, "") else None


def _int_param(request: Request, name: str) -> int | None:
    value = _param(request, name)
    try:
        return int(value) if value is not None else None
    except ValueError as exc:
        raise BadRequest(f"{name} must be an integer") from exc


def create_app(
    settings: Settings | None = None,
    tvdb: TVDBClient | None = None,
    overrides: OverrideStore | None = None,
    plex_transport: Any = None,
    imdb_transport: Any = None,
    tmdb_transport: Any = None,
) -> FastAPI:
    settings = settings or load_settings()
    owns_client = tvdb is None
    if tvdb is None:
        tvdb = TVDBClient(
            settings.tvdb_api_key,
            settings.tvdb_pin,
            base_url=settings.tvdb_base_url,
            cache=TTLCache(settings.cache_max_entries),
            ttl=settings.cache_ttl,
            max_concurrency=settings.max_concurrency,
            user_agent=f"tvdb-custom-agent/{__version__}",
        )
    overrides = overrides or OverrideStore(settings)
    imdb = (
        IMDbRatings(settings.data_dir, url=settings.imdb_dataset_url, refresh_hours=settings.imdb_refresh_hours,
                    transport=imdb_transport)
        if settings.imdb_ratings else None
    )
    tmdb = TMDBClient(settings.tmdb_api_key, transport=tmdb_transport) if settings.tmdb_api_key else None
    agent = Agent(settings, tvdb, overrides, imdb=imdb, tmdb=tmdb)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if not settings.tvdb_api_key:
            log.error("TVDB_API_KEY is not set - every metadata request will fail")
        if not settings.admin_token:
            log.warning("ADMIN_TOKEN is not set - the /admin page is open to anyone who can reach this server")
        for p in settings.all_profiles():
            log.info("Provider %-45s at %s/", p.identifier, p.base_path)
        if imdb is not None:
            imdb.start()  # loads/downloads the IMDb ratings dataset in the background
        log.info("Ratings: IMDb dataset %s, TMDB %s", "on" if imdb else "off", "on" if tmdb else "off (no TMDB_API_KEY)")
        yield
        if imdb is not None:
            await imdb.aclose()
        if tmdb is not None:
            await tmdb.aclose()
        if owns_client:
            await tvdb.aclose()

    app = FastAPI(title="TVDB custom metadata agent for Plex", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.agent = agent
    app.state.overrides = overrides
    app.add_middleware(ProfilePrefixMiddleware)

    # ------------------------------------------------------ error handling
    @app.exception_handler(NotFound)
    async def _not_found(_req: Request, exc: NotFound):
        return JSONResponse({"error": str(exc)}, status_code=404)

    @app.exception_handler(BadRequest)
    async def _bad_request(_req: Request, exc: BadRequest):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(OverrideStoreError)
    async def _store_error(_req: Request, exc: OverrideStoreError):
        log.error("%s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)

    @app.exception_handler(TVDBAuthError)
    async def _tvdb_auth(_req: Request, exc: TVDBAuthError):
        log.error("%s", exc)
        return JSONResponse({"error": "TVDB authentication failed; check TVDB_API_KEY / TVDB_PIN"}, status_code=500)

    @app.exception_handler(TVDBNotFound)
    async def _tvdb_not_found(_req: Request, exc: TVDBNotFound):
        return JSONResponse({"error": str(exc)}, status_code=404)

    @app.exception_handler(TVDBError)
    async def _tvdb_error(_req: Request, exc: TVDBError):
        log.error("%s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)

    @app.middleware("http")
    async def _log_requests(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        elapsed = (time.perf_counter() - started) * 1000
        log.info("%s %s%s -> %s (%.0f ms)", request.method, request.url.path,
                 f"?{request.url.query}" if request.url.query else "", response.status_code, elapsed)
        return response

    # ------------------------------------------------------------ helpers
    def profile_of(request: Request) -> Profile:
        name = request.path_params.get("profile")
        prof = settings.profile(name)
        if prof is None:
            raise NotFound(f"unknown profile '{name}'")
        return prof

    def ctx_of(request: Request, body: dict | None = None) -> Ctx:
        body = body or {}
        prof = profile_of(request)
        return agent.make_ctx(
            prof,
            language=_param(request, "X-Plex-Language") or body.get("X-Plex-Language"),
            country=_param(request, "X-Plex-Country") or body.get("X-Plex-Country"),
            episode_order=request.query_params.get("episodeOrder") or body.get("episodeOrder"),
        )

    def container(ctx: Ctx, items: list[dict], *, offset: int = 0, total: int | None = None,
                  element: str = "Metadata", custom: Customization | None = None) -> JSONResponse:
        if custom is not None and element == "Metadata":
            items = [custom.apply(i) for i in items]
        total = len(items) if total is None else total
        body = {
            "MediaContainer": {
                "offset": offset,
                "totalSize": total,
                "identifier": ctx.identifier,
                "size": len(items),
                element: items,
            }
        }
        headers = {"X-Plex-Container-Start": str(offset), "X-Plex-Container-Total-Size": str(total)}
        return JSONResponse(body, headers=headers)

    def paged(request: Request, ctx: Ctx, items: list[dict], custom: Customization) -> JSONResponse:
        start = max(0, _int_param(request, "X-Plex-Container-Start") or 0)
        size = _int_param(request, "X-Plex-Container-Size")
        if size is None:
            size = DEFAULT_CHILD_PAGE
        page = items[start : start + max(0, size)]
        return container(ctx, page, offset=start, total=len(items), custom=custom)

    # ------------------------------------------------------- provider API
    async def provider_root(request: Request):
        prof = profile_of(request)
        types = [{"type": t, "Scheme": [{"scheme": prof.identifier}]} for t in (2, 3, 4)]
        return JSONResponse({
            "MediaProvider": {
                "identifier": prof.identifier,
                "title": prof.title,
                "version": __version__,
                "Types": types,
                "Feature": [
                    # Relative to the provider URL: Plex appends feature keys to the
                    # URL it registered (verified on PMS 1.43.5).
                    {"type": "metadata", "key": "/library/metadata"},
                    {"type": "match", "key": "/library/metadata/matches"},
                ],
            }
        })

    async def get_metadata(request: Request, rating_key: str):
        ctx = ctx_of(request)
        include_children = request.query_params.get("includeChildren") in ("1", "true")
        md = await agent.metadata(rating_key, ctx, include_children)
        return container(ctx, [md], custom=Customization.from_params(dict(request.query_params)))

    async def get_children(request: Request, rating_key: str):
        ctx = ctx_of(request)
        items = await agent.children(rating_key, ctx)
        return paged(request, ctx, items, Customization.from_params(dict(request.query_params)))

    async def get_grandchildren(request: Request, rating_key: str):
        ctx = ctx_of(request)
        items = await agent.grandchildren(rating_key, ctx)
        return paged(request, ctx, items, Customization.from_params(dict(request.query_params)))

    async def get_images(request: Request, rating_key: str):
        ctx = ctx_of(request)
        images = await agent.images(rating_key, ctx)
        return container(ctx, images, element="Image")

    async def get_extras(request: Request, rating_key: str):
        # Plex asks for extras on every refresh; TVDB has none to offer.
        ctx = ctx_of(request)
        if keys.parse_key(rating_key) is None:
            raise NotFound(f"unknown ratingKey '{rating_key}'")
        return container(ctx, [])

    async def post_match(request: Request):
        raw = await request.body()
        body: dict[str, Any] = {}
        if raw:
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=False))
        if not isinstance(body, dict):
            raise BadRequest("match body must be a JSON object")
        # Accept hints passed as query parameters too.
        for k, v in request.query_params.items():
            body.setdefault(k, v)
        log.debug("match request: %s", body)
        ctx = ctx_of(request, body)
        results = await agent.match(body, ctx)
        return container(ctx, results, custom=Customization.from_params(body, dict(request.query_params)))

    for prefix in ("", "/profiles/{profile}"):
        roots = [prefix + "/"] if not prefix else [prefix, prefix + "/"]
        for root in roots:
            app.add_api_route(root, provider_root, methods=["GET"], include_in_schema=False)
        base = prefix + "/library/metadata"
        app.add_api_route(base + "/matches", post_match, methods=["POST", "PUT"], include_in_schema=False)
        app.add_api_route(base + "/{rating_key}", get_metadata, methods=["GET"], include_in_schema=False)
        app.add_api_route(base + "/{rating_key}/children", get_children, methods=["GET"], include_in_schema=False)
        app.add_api_route(base + "/{rating_key}/grandchildren", get_grandchildren, methods=["GET"], include_in_schema=False)
        app.add_api_route(base + "/{rating_key}/images", get_images, methods=["GET"], include_in_schema=False)
        app.add_api_route(base + "/{rating_key}/extras", get_extras, methods=["GET"], include_in_schema=False)

    # -------------------------------------------------------------- misc
    @app.get("/health", include_in_schema=False)
    async def health():
        return {
            "status": "ok",
            "version": __version__,
            "tvdbConfigured": bool(settings.tvdb_api_key),
            "imdbRatings": imdb.status() if imdb else None,
            "tmdbConfigured": tmdb is not None,
        }

    @app.get("/profiles", include_in_schema=False)
    async def list_profiles():
        return {"profiles": [_profile_info(p) for p in settings.all_profiles()]}

    # -------------------------------------------------------------- admin
    def require_admin(request: Request) -> None:
        if not settings.admin_token:
            return
        supplied = request.headers.get("X-Admin-Token") or request.query_params.get("token")
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            supplied = supplied or auth[7:].strip()
        if supplied != settings.admin_token:
            raise HTTPException(status_code=401, detail="admin token required")

    def scope_of(scope: str) -> str:
        if scope.lower() in GLOBAL_SCOPE_NAMES:
            return GLOBAL
        if settings.profile(scope) is None:
            raise HTTPException(status_code=404, detail=f"unknown profile '{scope}'")
        return scope

    @app.get("/admin", include_in_schema=False)
    async def admin_page():
        html = resources.files("tvdb_agent").joinpath("static/admin.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    @app.get("/admin/api/status", include_in_schema=False)
    async def admin_status(request: Request):
        require_admin(request)
        return {
            "version": __version__,
            "tvdbConfigured": bool(settings.tvdb_api_key),
            "authRequired": bool(settings.admin_token),
            "publicUrl": settings.public_url,
            "profiles": [_profile_info(p) for p in settings.all_profiles()],
            "orders": [{"slug": o.slug, "plexId": o.plex_id, "title": o.title} for o in orders.ORDERS],
            "cacheEntries": len(agent.tvdb.cache),
        }

    @app.get("/admin/api/overrides", include_in_schema=False)
    async def admin_overrides(request: Request):
        require_admin(request)
        return {"overrides": overrides.listing()}

    @app.put("/admin/api/overrides/{scope}/{series_id}", include_in_schema=False)
    async def admin_set_override(request: Request, scope: str, series_id: int):
        require_admin(request)
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            payload = {}
        order = (payload or {}).get("order") or request.query_params.get("order")
        if not order:
            raise HTTPException(status_code=400, detail="body must contain {\"order\": \"dvd\"}")
        try:
            stored = overrides.set(scope_of(scope), series_id, str(order))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"scope": scope, "seriesId": series_id, "order": stored}

    @app.delete("/admin/api/overrides/{scope}/{series_id}", include_in_schema=False)
    async def admin_delete_override(request: Request, scope: str, series_id: int):
        require_admin(request)
        removed = overrides.delete(scope_of(scope), series_id)
        if not removed:
            raise HTTPException(status_code=404, detail="no runtime override for that show and scope")
        return Response(status_code=204)

    @app.get("/admin/api/search", include_in_schema=False)
    async def admin_search(request: Request, q: str, year: int | None = None):
        require_admin(request)
        ctx = agent.make_ctx(settings.root, None, None, None)  # type: ignore[arg-type]
        found = await agent._search(q, year, ctx)
        return {"results": [agent._search_result_md(row, sid, sc, ctx) | {"seriesId": sid} for sid, sc, row in found[:20]]}

    @app.get("/admin/api/series/{series_id}", include_in_schema=False)
    async def admin_series(request: Request, series_id: int):
        require_admin(request)
        root_ctx = agent.make_ctx(settings.root, None, None, None)  # type: ignore[arg-type]
        series = await agent.series(series_id, root_ctx)
        available = orders.series_available_orders(series)
        names = orders.season_type_names(series)
        order_rows = []
        for slug in available:
            eps = await agent.episodes(series, slug, root_ctx)
            seasons = agent.season_infos(series, slug, eps)
            order_rows.append({
                "slug": slug,
                "plexId": orders.BY_SLUG[slug].plex_id if slug in orders.BY_SLUG else None,
                "name": names.get(slug) or slug,
                "seasons": len([s for s in seasons if s.number != 0]),
                "episodes": len(eps),
            })
        effective = []
        for prof in settings.all_profiles():
            pctx = agent.make_ctx(prof, None, None, None)
            used, _, reason = agent.resolve_order(series, pctx)
            effective.append({"profile": prof.name or "", "identifier": prof.identifier, "order": used, "reason": reason})
        title, _ = agent._show_title(series, root_ctx)
        return {
            "seriesId": series_id,
            "title": title,
            "year": series.get("year"),
            "defaultOrder": orders.series_default_order(series),
            "orders": order_rows,
            "effective": effective,
        }

    @app.get("/admin/api/plex", include_in_schema=False)
    async def admin_plex_info(request: Request):
        require_admin(request)
        defaults = set(default_profile_names(settings))
        return {
            "plexUrl": settings.plex_url,
            "tokenConfigured": bool(settings.plex_token),
            "agentUrl": settings.public_url,
            "profiles": [
                {
                    "name": p.name or "",
                    "agent": agent_title(settings, p),
                    "identifier": p.identifier,
                    "path": p.base_path or "/",
                    "episodeOrder": p.episode_order,
                    "default": (p.name or "") in defaults,
                }
                for p in settings.all_profiles()
            ],
        }

    @app.post("/admin/api/plex/register", include_in_schema=False)
    async def admin_plex_register(request: Request):
        """Create/update one Plex agent per profile, so each ordering can be
        picked in a library's Agent setting."""
        require_admin(request)
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        plex_url = (payload.get("plexUrl") or settings.plex_url or "").strip()
        token = (payload.get("token") or settings.plex_token or "").strip() or None
        agent_url = (payload.get("agentUrl") or settings.public_url or "").strip()
        if not plex_url:
            raise HTTPException(status_code=400, detail="plexUrl is required (or set PLEX_URL)")
        if not agent_url:
            raise HTTPException(status_code=400, detail="agentUrl is required (or set PUBLIC_URL)")
        names = payload.get("profiles")
        try:
            profiles = resolve_profiles(settings, names if isinstance(names, list) else None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        client = PlexClient(plex_url, token, transport=plex_transport)
        try:
            results = await register(client, settings, profiles, agent_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PlexError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            await client.aclose()
        return {"ok": all(r.error is None for r in results), "results": [r.as_dict() for r in results]}

    @app.post("/admin/api/cache/clear", include_in_schema=False)
    async def admin_clear_cache(request: Request, series_id: int | None = None):
        require_admin(request)
        if series_id is None:
            count = len(agent.tvdb.cache)
            agent.tvdb.cache.clear()
        else:
            needle = f"/series/{series_id}/"
            count = agent.tvdb.cache.invalidate(lambda k: any(needle in str(part) for part in (k if isinstance(k, tuple) else (k,))))
        return {"cleared": count}

    return app


def _profile_info(p: Profile) -> dict:
    return {
        "name": p.name or "",
        "identifier": p.identifier,
        "title": p.title,
        "path": p.base_path or "/",
        "episodeOrder": p.episode_order,
        "language": p.language,
        "builtin": p.builtin,
        "honorPlexEpisodeOrder": p.honor_plex_episode_order,
    }
