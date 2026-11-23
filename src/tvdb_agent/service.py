"""Builds Plex metadata responses from TVDB data."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from . import keys, mapper, matching, orders
from .config import Profile, Settings
from .i18n import CountryTable, to_tvdb_language
from .imdb import IMDbRatings
from .tmdb import TMDBClient, tmdb_rating
from .overrides import OverrideStore
from .tvdb import TVDBClient, TVDBError, TVDBNotFound

log = logging.getLogger(__name__)

SHOW, SEASON, EPISODE = 2, 3, 4
THEME_URL = "https://tvthemes.plexapp.com/{id}.mp3"


class NotFound(Exception):
    pass


class BadRequest(Exception):
    pass


@dataclass
class Ctx:
    """Per-request context."""

    profile: Profile
    lang: str
    langs: list[str]
    country: str | None = None
    plex_order: str | None = None  # parsed episodeOrder from Plex

    @property
    def identifier(self) -> str:
        return self.profile.identifier

    def path(self, rating_key: str, suffix: str = "") -> str:
        # Relative to the provider URL, like the feature keys.
        return f"/library/metadata/{rating_key}{suffix}"

    def guid(self, kind: str, rating_key: str) -> str:
        return keys.guid(self.identifier, kind, rating_key)


@dataclass
class SeasonInfo:
    number: int
    record: dict | None
    episodes: list[dict] = field(default_factory=list)


class Agent:
    def __init__(
        self,
        settings: Settings,
        tvdb: TVDBClient,
        overrides: OverrideStore,
        imdb: IMDbRatings | None = None,
        tmdb: TMDBClient | None = None,
    ) -> None:
        self.settings = settings
        self.tvdb = tvdb
        self.overrides = overrides
        self.imdb = imdb
        self.tmdb = tmdb
        self.countries = CountryTable()
        self._art_types: dict[int, tuple[str, str | None]] | None = None
        self._countries_loaded = False
        self._static_retry_at = 0.0
        self._static_lock = asyncio.Lock()

    # ------------------------------------------------------------ context
    def make_ctx(
        self,
        profile: Profile,
        language: str | None,
        country: str | None,
        episode_order: str | None,
    ) -> Ctx:
        lang = None
        if not profile.force_language:
            lang = to_tvdb_language(language)
        lang = lang or profile.language or self.settings.language
        langs = [lang]
        for fb in self.settings.fallback_languages:
            if fb not in langs:
                langs.append(fb)
        return Ctx(
            profile=profile,
            lang=lang,
            langs=langs,
            country=(country or "").strip().lower() or None,
            plex_order=orders.parse_order(episode_order),
        )

    async def _static(self) -> None:
        """Load TVDB lookup tables once; on failure use built-in tables and
        retry a few minutes later."""
        if self._art_types is not None and self._countries_loaded:
            return
        if time.monotonic() < self._static_retry_at:
            return
        async with self._static_lock:
            failed = False
            if self._art_types is None:
                try:
                    self._art_types = mapper.build_artwork_type_map(await self.tvdb.artwork_types())
                except TVDBError as exc:
                    failed = True
                    log.warning("Could not load TVDB artwork types, using built-in table: %s", exc)
            if not self._countries_loaded:
                try:
                    self.countries.load(await self.tvdb.countries())
                    self._countries_loaded = True
                except TVDBError as exc:
                    failed = True
                    log.warning("Could not load TVDB countries, using built-in table: %s", exc)
            if failed:
                self._static_retry_at = time.monotonic() + 300

    @property
    def art_types(self) -> dict[int, tuple[str, str | None]]:
        return self._art_types or mapper.FALLBACK_ARTWORK_TYPES

    # --------------------------------------------------------- TVDB access
    async def series(self, series_id: int, ctx: Ctx) -> dict:
        await self._static()
        try:
            return await self.tvdb.series_extended(series_id, ctx.lang)
        except TVDBNotFound as exc:
            raise NotFound(f"TVDB series {series_id} not found") from exc

    async def episodes(self, series: dict, order: str, ctx: Ctx) -> list[dict]:
        """Episodes of ``series`` in ``order`` with translated name/overview
        stored as ``_name``/``_overview``."""
        sid = int(series["id"])
        base = await self.tvdb.series_episodes(sid, order)
        original = series.get("originalLanguage")
        translated: list[dict[int, dict]] = []
        for lang in ctx.langs:
            if lang == original:
                break
            try:
                rows = await self.tvdb.series_episodes(sid, order, lang)
            except TVDBError as exc:
                log.debug("No %s translations for series %s: %s", lang, sid, exc)
                continue
            translated.append({r.get("id"): r for r in rows if r.get("id") is not None})
        result = []
        for ep in base:
            ep = dict(ep)
            name = overview = None
            for table in translated:
                tr = table.get(ep.get("id"))
                if not tr:
                    continue
                name = name or (tr.get("name") or "").strip() or None
                overview = overview or (tr.get("overview") or "").strip() or None
            ep["_name"] = name or (ep.get("name") or "").strip() or None
            ep["_overview"] = overview or (ep.get("overview") or "").strip() or None
            if ep.get("seasonNumber") is None and order == "absolute":
                ep["seasonNumber"] = 1
            if ep.get("number") is None and order == "absolute":
                ep["number"] = ep.get("absoluteNumber")
            if ep.get("seasonNumber") is None or ep.get("number") is None:
                continue
            result.append(ep)
        result.sort(key=lambda e: (int(e["seasonNumber"]), int(e["number"]), e.get("id") or 0))
        return result

    # ------------------------------------------------------ order selection
    def resolve_order(self, series: dict, ctx: Ctx) -> tuple[str, str, str]:
        """Return ``(order_used, configured_order, reason)``.

        Precedence: per-show override > Plex ``episodeOrder`` > profile
        (library) default > series default.
        """
        sid = int(series["id"])
        available = orders.series_available_orders(series)
        default = orders.series_default_order(series)

        def concrete(value: str) -> str:
            return default if value == orders.DEFAULT else value

        override, source = self.overrides.lookup(ctx.profile.name, sid)
        configured = concrete(override or ctx.profile.episode_order)
        reason = f"override ({source})" if override else "profile default"
        if configured not in available and ctx.profile.fallback_to_default_order:
            reason += f"; '{configured}' unavailable, using series default"
            configured = default
        if override:
            return configured, configured, reason
        if ctx.profile.honor_plex_episode_order and ctx.plex_order:
            return concrete(ctx.plex_order), configured, "plex episodeOrder"
        return configured, configured, reason

    # ----------------------------------------------------------- builders
    def _show_title(self, series: dict, ctx: Ctx) -> tuple[str, str | None]:
        title = mapper.translation(series, "name", ctx.langs) or series.get("name") or ""
        original_lang = series.get("originalLanguage")
        original = None
        if original_lang:
            original = mapper.translation(series, "name", [original_lang]) or series.get("name")
        if original and original == title:
            original = None
        return title, original

    def _show_ref(self, series: dict, ctx: Ctx) -> dict:
        """Fields describing the show that children need."""
        sid = int(series["id"])
        rk = keys.show_key(sid)
        title, _ = self._show_title(series, ctx)
        images = self._series_images(series, ctx)
        return {
            "ratingKey": rk,
            "key": ctx.path(rk),
            "guid": ctx.guid("show", rk),
            "title": title,
            "thumb": _first_url(images.get("coverPoster")) or mapper.image_url(series.get("image")),
            "art": _first_url(images.get("background")),
        }

    def _series_images(self, series: dict, ctx: Ctx, per_type: int | None = 1) -> dict[str, list[dict]]:
        title = mapper.translation(series, "name", ctx.langs) or series.get("name")
        return mapper.select_images(
            series.get("artworks") or [],
            self.art_types,
            "series",
            ctx.langs,
            series.get("originalLanguage"),
            alt=title,
            per_type=per_type,
        )

    def show_metadata(self, series: dict, ctx: Ctx, configured_order: str, full: bool = True) -> dict:
        sid = int(series["id"])
        rk = keys.show_key(sid)
        title, original_title = self._show_title(series, ctx)
        images = self._series_images(series, ctx)
        md: dict[str, Any] = {
            "ratingKey": rk,
            "key": ctx.path(rk, "/children"),
            "guid": ctx.guid("show", rk),
            "type": "show",
            "title": title,
        }
        date = mapper.iso_date(series.get("firstAired"))
        if date:
            md["originallyAvailableAt"] = date
        year = matching.year_of(series.get("year")) or matching.year_of(date)
        if year:
            md["year"] = year
        thumb = _first_url(images.get("coverPoster")) or mapper.image_url(series.get("image"))
        if thumb:
            md["thumb"] = thumb
        art = _first_url(images.get("background"))
        if art:
            md["art"] = art
        if original_title:
            md["originalTitle"] = original_title
        summary = mapper.translation(series, "overview", ctx.langs) or series.get("overview")
        if summary:
            md["summary"] = summary.strip()
        if not full:
            return md

        rating = mapper.content_rating(series.get("contentRatings"), ctx.country, self.countries)
        if rating:
            md["contentRating"] = rating
        duration = mapper.minutes_to_ms(series.get("averageRuntime"))
        if duration:
            md["duration"] = duration
        studios = mapper.companies_of(series.get("companies"), "Studio", "Production Company")
        if studios:
            md["studio"] = studios[0]
        if self.settings.theme_music:
            md["theme"] = THEME_URL.format(id=sid)

        md["Image"] = mapper.flatten_images(images)
        originals = []
        if series.get("originalLanguage") and series.get("originalLanguage") != ctx.lang:
            originals = mapper.original_images(
                series.get("artworks") or [], self.art_types, "series", series.get("originalLanguage"), original_title or title
            )
        if originals:
            md["OriginalImage"] = originals
        md["Genre"] = mapper.tags(g.get("name") for g in series.get("genres") or [])
        md["Guid"] = [{"id": f"tvdb://{sid}"}, *mapper.external_guids(series.get("remoteIds"))]
        country = self.countries.name(series.get("originalCountry") or series.get("country"))
        if country:
            md["Country"] = [{"tag": country}]
        md.update(mapper.people(series.get("characters") or [], self.settings.max_cast))
        networks = [n.get("name") for n in (series.get("originalNetwork"), series.get("latestNetwork")) if n]
        md["Network"] = mapper.tags(networks)
        md["Studio"] = mapper.tags(studios)
        md["SeasonType"] = orders.plex_season_types(series, first=configured_order)
        return _prune(md)

    def season_infos(self, series: dict, order: str, episodes: list[dict]) -> list[SeasonInfo]:
        records = {}
        for season in series.get("seasons") or []:
            if orders.season_type_slug(season.get("type")) != order or season.get("number") is None:
                continue
            records.setdefault(int(season["number"]), season)
        infos: dict[int, SeasonInfo] = {n: SeasonInfo(n, rec) for n, rec in records.items()}
        for ep in episodes:
            n = int(ep["seasonNumber"])
            infos.setdefault(n, SeasonInfo(n, None)).episodes.append(ep)
        return [infos[n] for n in sorted(infos)]

    def _season_title(self, info: SeasonInfo) -> str:
        if self.settings.season_titles and info.record and (info.record.get("name") or "").strip():
            return info.record["name"].strip()
        if info.number == 0:
            return "Specials"
        return f"Season {info.number}"

    def season_metadata(self, series: dict, order: str, info: SeasonInfo, ctx: Ctx, show: dict | None = None) -> dict:
        sid = int(series["id"])
        show = show or self._show_ref(series, ctx)
        rk = keys.season_key(sid, order, info.number)
        title = self._season_title(info)
        md: dict[str, Any] = {
            "ratingKey": rk,
            "key": ctx.path(rk, "/children"),
            "guid": ctx.guid("season", rk),
            "type": "season",
            "title": title,
            "index": info.number,
            "parentRatingKey": show["ratingKey"],
            "parentKey": show["key"],
            "parentGuid": show["guid"],
            "parentType": "show",
            "parentTitle": show["title"],
            "parentThumb": show.get("thumb"),
            "parentArt": show.get("art"),
        }
        dates = sorted(d for d in (mapper.iso_date(e.get("aired")) for e in info.episodes) if d)
        if dates:
            md["originallyAvailableAt"] = dates[0]
            md["year"] = int(dates[0][:4])
        elif info.record and matching.year_of(info.record.get("year")):
            md["year"] = matching.year_of(info.record.get("year"))
        season_images: dict[str, list[dict]] = {}
        if info.record:
            season_arts = [a for a in series.get("artworks") or [] if a.get("seasonId") == info.record.get("id")]
            season_images = mapper.select_images(
                season_arts, self.art_types, "season", ctx.langs, series.get("originalLanguage"), alt=title
            )
        thumb = _first_url(season_images.get("coverPoster")) or (
            mapper.image_url(info.record.get("image")) if info.record else None
        )
        if thumb:
            md["thumb"] = thumb
            season_images.setdefault("coverPoster", [{"type": "coverPoster", "url": thumb, "alt": title}])
        art = _first_url(season_images.get("background")) or show.get("art")
        if art:
            md["art"] = art
            season_images.setdefault("background", [{"type": "background", "url": art, "alt": title}])
        md["Image"] = mapper.flatten_images(season_images)
        if info.record and info.record.get("id"):
            md["Guid"] = [{"id": f"tvdb://{info.record['id']}"}]
        rating = mapper.content_rating(series.get("contentRatings"), ctx.country, self.countries)
        if rating:
            md["contentRating"] = rating
        return _prune(md)

    def episode_metadata(
        self,
        series: dict,
        order: str,
        ep: dict,
        ctx: Ctx,
        show: dict | None = None,
        season: dict | None = None,
        extended: dict | None = None,
    ) -> dict:
        sid = int(series["id"])
        show = show or self._show_ref(series, ctx)
        season_no = int(ep["seasonNumber"])
        number = int(ep["number"])
        season_rk = keys.season_key(sid, order, season_no)
        rk = keys.episode_key(sid, order, int(ep["id"]))
        if season is None:
            season = {
                "title": "Specials" if season_no == 0 else f"Season {season_no}",
                "thumb": None,
            }
        title = ep.get("_name") or ep.get("name") or f"Episode {number}"
        original_title = (ep.get("name") or "").strip()
        md: dict[str, Any] = {
            "ratingKey": rk,
            "key": ctx.path(rk),
            "guid": ctx.guid("episode", rk),
            "type": "episode",
            "title": title,
            "index": number,
            "parentIndex": season_no,
            "parentRatingKey": season_rk,
            "parentKey": ctx.path(season_rk),
            "parentGuid": ctx.guid("season", season_rk),
            "parentType": "season",
            "parentTitle": season.get("title"),
            "parentThumb": season.get("thumb"),
            "grandparentRatingKey": show["ratingKey"],
            "grandparentKey": show["key"],
            "grandparentGuid": show["guid"],
            "grandparentType": "show",
            "grandparentTitle": show["title"],
            "grandparentThumb": show.get("thumb"),
            "grandparentArt": show.get("art"),
        }
        if original_title and original_title != title and series.get("originalLanguage") not in ctx.langs[:1]:
            md["originalTitle"] = original_title
        date = mapper.iso_date(ep.get("aired"))
        if date:
            md["originallyAvailableAt"] = date
            md["year"] = int(date[:4])
        summary = ep.get("_overview") or ep.get("overview")
        if summary:
            md["summary"] = summary.strip()
        duration = mapper.minutes_to_ms(ep.get("runtime"))
        if duration:
            md["duration"] = duration
        thumb = mapper.image_url(ep.get("image"))
        if thumb:
            md["thumb"] = thumb
            md["Image"] = [{"type": "snapshot", "url": thumb, "alt": title}]
        md["Guid"] = [{"id": f"tvdb://{ep['id']}"}]
        rating = None
        if extended:
            rating = mapper.content_rating(extended.get("contentRatings"), ctx.country, self.countries)
            md["Guid"].extend(mapper.external_guids(extended.get("remoteIds")))
            crew = mapper.people(extended.get("characters") or [], self.settings.max_cast)
            series_cast = mapper.people(series.get("characters") or [], self.settings.max_cast).get("Role", [])
            roles = crew.pop("Role", [])
            names = {r["tag"] for r in roles}
            for r in series_cast:
                if len(roles) >= self.settings.max_cast:
                    break
                if r["tag"] not in names:
                    roles.append(dict(r))
            for i, r in enumerate(roles, start=1):
                r["order"] = i
            if roles:
                md["Role"] = roles
            md.update(crew)
            if not ep.get("_name"):
                name = mapper.translation(extended, "name", ctx.langs)
                if name:
                    md["title"] = name
            if not ep.get("_overview"):
                overview = mapper.translation(extended, "overview", ctx.langs)
                if overview:
                    md["summary"] = overview
        rating = rating or mapper.content_rating(series.get("contentRatings"), ctx.country, self.countries)
        if rating:
            md["contentRating"] = rating
        return _prune(md)

    # -------------------------------------------------------- operations
    async def _show_context(self, series_id: int, ctx: Ctx, order: str | None = None):
        series = await self.series(series_id, ctx)
        if order is None:
            order, configured, reason = self.resolve_order(series, ctx)
        else:
            configured, reason = order, "ratingKey"
        log.debug("series %s: order=%s (%s)", series_id, order, reason)
        episodes = await self.episodes(series, order, ctx)
        seasons = self.season_infos(series, order, episodes)
        return series, order, configured, episodes, seasons

    async def metadata(self, rating_key: str, ctx: Ctx, include_children: bool) -> dict:
        key = keys.parse_key(rating_key)
        if key is None:
            raise NotFound(f"unknown ratingKey '{rating_key}'")
        if key.kind == "show":
            return await self.show_item(key.series_id, ctx, include_children)
        if key.kind == "season":
            return await self.season_item(key.series_id, key.order, key.season, ctx, include_children)  # type: ignore[arg-type]
        return await self.episode_item(key.series_id, key.order, key.episode_id, ctx)  # type: ignore[arg-type]

    async def show_item(self, series_id: int, ctx: Ctx, include_children: bool) -> dict:
        if not include_children:
            series = await self.series(series_id, ctx)
            _, configured, _ = self.resolve_order(series, ctx)
            return await self._enrich_show(self.show_metadata(series, ctx, configured), series_id)
        series, order, configured, _, seasons = await self._show_context(series_id, ctx)
        md = await self._enrich_show(self.show_metadata(series, ctx, configured), series_id)
        show = self._show_ref(series, ctx)
        children = [self.season_metadata(series, order, s, ctx, show) for s in seasons]
        md["Children"] = {"size": len(children), "Metadata": children}
        return md

    def _check_order(self, order: str | None) -> str:
        if order not in orders.BY_SLUG:
            raise NotFound(f"unknown episode order '{order}'")
        return order  # type: ignore[return-value]

    async def season_item(self, series_id: int, order: str, number: int, ctx: Ctx, include_children: bool) -> dict:
        order = self._check_order(order)
        series, order, _, _, seasons = await self._show_context(series_id, ctx, order)
        info = next((s for s in seasons if s.number == number), None)
        if info is None:
            raise NotFound(f"series {series_id} has no season {number} in {order} order")
        show = self._show_ref(series, ctx)
        md = self.season_metadata(series, order, info, ctx, show)
        if info.record and info.record.get("id"):
            summary = await self._season_summary(int(info.record["id"]), ctx)
            if summary:
                md["summary"] = summary
        if include_children:
            season_ref = {"title": md["title"], "thumb": md.get("thumb")}
            details = await self._episode_details(info.episodes)
            eps = [
                self.episode_metadata(series, order, e, ctx, show, season_ref, details.get(int(e["id"])))
                for e in info.episodes
            ]
            await self._enrich_episodes(eps)
            md["Children"] = {"size": len(eps), "Metadata": eps}
        return md

    async def _episode_details(self, episodes: list[dict]) -> dict[int, dict]:
        """Extended TVDB records (cast, crew, IMDb/TMDB ids, ratings) for a
        season's episodes. Plex refreshes a show by fetching each season with
        includeChildren=1 and never asks for single episodes, so the season's
        children have to carry the full episode metadata."""
        if not self.settings.episode_details or not episodes:
            return {}

        async def one(episode_id: int) -> tuple[int, dict | None]:
            try:
                return episode_id, await self.tvdb.episode_extended(episode_id)
            except TVDBError as exc:
                log.debug("episode %s extended unavailable: %s", episode_id, exc)
                return episode_id, None

        results = await asyncio.gather(*(one(int(e["id"])) for e in episodes))
        return {eid: rec for eid, rec in results if rec}

    async def _season_summary(self, season_id: int, ctx: Ctx) -> str | None:
        for lang in ctx.langs:
            try:
                tr = await self.tvdb.season_translation(season_id, lang)
            except TVDBError:
                return None
            if tr and (tr.get("overview") or "").strip():
                return tr["overview"].strip()
        return None

    async def episode_item(self, series_id: int, order: str, episode_id: int, ctx: Ctx) -> dict:
        order = self._check_order(order)
        series, order, _, episodes, seasons = await self._show_context(series_id, ctx, order)
        ep = next((e for e in episodes if int(e["id"]) == int(episode_id)), None)
        if ep is None:
            raise NotFound(f"episode {episode_id} is not part of series {series_id} in {order} order")
        info = next((s for s in seasons if s.number == int(ep["seasonNumber"])), None)
        show = self._show_ref(series, ctx)
        season_ref = None
        if info is not None:
            season_md = self.season_metadata(series, order, info, ctx, show)
            season_ref = {"title": season_md["title"], "thumb": season_md.get("thumb")}
        try:
            extended = await self.tvdb.episode_extended(int(episode_id))
        except TVDBError as exc:
            log.debug("episode %s extended unavailable: %s", episode_id, exc)
            extended = None
        md = self.episode_metadata(series, order, ep, ctx, show, season_ref, extended)
        await self._enrich_episodes([md])
        return md

    # ---------------------------------------------------- ratings / ids
    async def _enrich_show(self, md: dict, series_id: int) -> dict:
        """Add IMDb/TMDB ratings and any TMDB/IMDb ids TVDB lacks."""
        try:
            tmdb_value = None
            if self.tmdb is not None:
                known = _guid_map(md)
                tv: dict = {}
                if known.get("tmdb"):
                    tv = await self.tmdb.tv(known["tmdb"])
                else:
                    found = await self.tmdb.find_by_tvdb(series_id)
                    hit = next(iter(found.get("tv_results") or []), None)
                    if hit:
                        tv = await self.tmdb.tv(hit["id"]) or hit
                if tv:
                    _add_guid(md, "tmdb", tv.get("id"))
                    _add_guid(md, "imdb", (tv.get("external_ids") or {}).get("imdb_id"))
                    tmdb_value = tmdb_rating(tv)
            self._set_ratings(md, tmdb_value)
        except Exception as exc:  # ratings are optional; never fail the request
            log.warning("Could not add ratings for series %s: %s", series_id, exc)
        return md

    async def _enrich_episodes(self, mds: list[dict]) -> list[dict]:
        use_tmdb = self.tmdb is not None and self.settings.episode_details

        async def one(md: dict) -> None:
            try:
                tmdb_value = None
                tvdb_id = _guid_map(md).get("tvdb")
                if use_tmdb and tvdb_id and tvdb_id.isdigit():
                    found = await self.tmdb.find_by_tvdb(int(tvdb_id))  # type: ignore[union-attr]
                    hit = next(iter(found.get("tv_episode_results") or []), None)
                    if hit:
                        _add_guid(md, "tmdb", hit.get("id"))
                        tmdb_value = tmdb_rating(hit)
                        if "imdb" not in _guid_map(md) and hit.get("show_id") is not None:
                            ext = await self.tmdb.episode_external_ids(  # type: ignore[union-attr]
                                hit["show_id"], hit.get("season_number", 0), hit.get("episode_number", 0)
                            )
                            _add_guid(md, "imdb", ext.get("imdb_id"))
                self._set_ratings(md, tmdb_value)
            except Exception as exc:
                log.warning("Could not add ratings for %s: %s", md.get("ratingKey"), exc)

        if self.imdb is not None or use_tmdb:
            await asyncio.gather(*(one(md) for md in mds))
        return mds

    def _set_ratings(self, md: dict, tmdb_value: float | None) -> None:
        ratings = []
        imdb_value = self.imdb.rating(_guid_map(md).get("imdb")) if self.imdb is not None else None
        if imdb_value is not None:
            ratings.append({"image": "imdb://image.rating", "type": "audience", "value": imdb_value})
        if tmdb_value is not None:
            ratings.append({"image": "themoviedb://image.rating", "type": "audience", "value": tmdb_value})
        if ratings:
            md["Rating"] = ratings

    async def children(self, rating_key: str, ctx: Ctx) -> list[dict]:
        key = keys.parse_key(rating_key)
        if key is None:
            raise NotFound(f"unknown ratingKey '{rating_key}'")
        if key.kind == "show":
            series, order, _, _, seasons = await self._show_context(key.series_id, ctx)
            show = self._show_ref(series, ctx)
            return [self.season_metadata(series, order, s, ctx, show) for s in seasons]
        if key.kind == "season":
            order = self._check_order(key.order)
            series, order, _, _, seasons = await self._show_context(key.series_id, ctx, order)
            info = next((s for s in seasons if s.number == key.season), None)
            if info is None:
                raise NotFound(f"series {key.series_id} has no season {key.season} in {order} order")
            show = self._show_ref(series, ctx)
            season_md = self.season_metadata(series, order, info, ctx, show)
            season_ref = {"title": season_md["title"], "thumb": season_md.get("thumb")}
            details = await self._episode_details(info.episodes)
            eps = [
                self.episode_metadata(series, order, e, ctx, show, season_ref, details.get(int(e["id"])))
                for e in info.episodes
            ]
            return await self._enrich_episodes(eps)
        return []

    async def grandchildren(self, rating_key: str, ctx: Ctx) -> list[dict]:
        key = keys.parse_key(rating_key)
        if key is None:
            raise NotFound(f"unknown ratingKey '{rating_key}'")
        if key.kind != "show":
            return []
        series, order, _, _, seasons = await self._show_context(key.series_id, ctx)
        show = self._show_ref(series, ctx)
        out = []
        for info in seasons:
            season_md = self.season_metadata(series, order, info, ctx, show)
            season_ref = {"title": season_md["title"], "thumb": season_md.get("thumb")}
            out.extend(self.episode_metadata(series, order, e, ctx, show, season_ref) for e in info.episodes)
        return out

    async def images(self, rating_key: str, ctx: Ctx) -> list[dict]:
        key = keys.parse_key(rating_key)
        if key is None:
            raise NotFound(f"unknown ratingKey '{rating_key}'")
        series = await self.series(key.series_id, ctx)
        if key.kind == "show":
            return mapper.flatten_images(self._series_images(series, ctx, per_type=None))
        order = self._check_order(key.order)
        if key.kind == "season":
            md = await self.season_item(key.series_id, order, key.season, ctx, False)  # type: ignore[arg-type]
            record = next(
                (
                    s for s in series.get("seasons") or []
                    if orders.season_type_slug(s.get("type")) == order and s.get("number") == key.season
                ),
                None,
            )
            arts = [a for a in series.get("artworks") or [] if record and a.get("seasonId") == record.get("id")]
            if record and record.get("id"):
                try:
                    extra = (await self.tvdb.season_extended(int(record["id"]))).get("artwork") or []
                    known = {a.get("id") for a in arts}
                    arts.extend(a for a in extra if a.get("id") not in known)
                except TVDBError:
                    pass
            grouped = mapper.select_images(
                arts, self.art_types, "season", ctx.langs, series.get("originalLanguage"), alt=md["title"], per_type=None
            )
            images = mapper.flatten_images(grouped)
            return images or md.get("Image", [])
        md = await self.episode_item(key.series_id, order, key.episode_id, ctx)  # type: ignore[arg-type]
        return md.get("Image", [])

    # ------------------------------------------------------------- match
    async def match(self, body: dict, ctx: Ctx) -> list[dict]:
        try:
            mtype = int(body.get("type"))
        except (TypeError, ValueError) as exc:
            raise BadRequest("match requires a numeric 'type'") from exc
        manual = _flag(body.get("manual"))
        include_children = _flag(body.get("includeChildren"))
        if mtype == SHOW:
            return await self._match_show(body, ctx, manual, include_children)
        if mtype == SEASON:
            return await self._match_season(body, ctx, include_children)
        if mtype == EPISODE:
            return await self._match_episode(body, ctx, manual)
        raise BadRequest(f"unsupported metadata type {mtype}; this provider handles TV shows (2, 3, 4)")

    async def _series_from_hint(self, hint: keys.IdHint, kind: str = "show") -> int | None:
        """Resolve an id hint to a TVDB series id. ``kind`` is the type the
        hint refers to (show/season/episode) for bare ``tvdb://`` ids."""
        try:
            if hint.source == "own":
                parsed = keys.parse_key(hint.value)
                return parsed.series_id if parsed else None
            if hint.source == "tvdb":
                value = int(hint.value)
                if kind == "season":
                    return int((await self.tvdb.season_extended(value)).get("seriesId"))
                if kind == "episode":
                    return int((await self.tvdb.episode(value)).get("seriesId"))
                return value
            if hint.source in ("imdb", "tmdb"):
                for row in await self.tvdb.search_remote_id(hint.value):
                    if row.get("series") and row["series"].get("id"):
                        return int(row["series"]["id"])
                    if kind == "episode" and row.get("episode") and row["episode"].get("seriesId"):
                        return int(row["episode"]["seriesId"])
        except (TVDBError, TypeError, ValueError) as exc:
            log.debug("Could not resolve hint %s: %s", hint, exc)
        return None

    async def _search(self, title: str, year: int | None, ctx: Ctx) -> list[tuple[int, int, dict]]:
        """Search TVDB; returns ``(series_id, score, search_result)``."""
        clean, title_year = matching.split_year(keys.strip_tags(title))
        year = year or title_year
        if not clean:
            return []
        try:
            results = await self.tvdb.search_series(clean, year) if year else []
            if not results:
                results = await self.tvdb.search_series(clean)
        except TVDBError as exc:
            log.warning("TVDB search for %r failed: %s", clean, exc)
            return []
        scored: dict[int, tuple[int, int, dict]] = {}
        for rank, row in enumerate(results):
            try:
                sid = int(row.get("tvdb_id") or str(row.get("id", "")).removeprefix("series-"))
            except ValueError:
                continue
            names = [row.get("name") or "", *(row.get("aliases") or []), *((row.get("translations") or {}).values())]
            cand_year = matching.year_of(row.get("year")) or matching.year_of(row.get("first_air_time"))
            sc = matching.score(clean, year, names, cand_year, rank)
            if sid not in scored or scored[sid][1] < sc:
                scored[sid] = (sid, sc, row)
        return sorted(scored.values(), key=lambda t: -t[1])

    def _search_result_md(self, row: dict, sid: int, score: int, ctx: Ctx) -> dict:
        rk = keys.show_key(sid)
        translations = row.get("translations") or {}
        overviews = row.get("overviews") or {}
        title = next((translations[l] for l in ctx.langs if translations.get(l)), None) or row.get("name")
        md: dict[str, Any] = {
            "ratingKey": rk,
            "key": ctx.path(rk, "/children"),
            "guid": ctx.guid("show", rk),
            "type": "show",
            "title": title,
            "score": score,
        }
        year = matching.year_of(row.get("year"))
        if year:
            md["year"] = year
        date = mapper.iso_date(row.get("first_air_time"))
        if date:
            md["originallyAvailableAt"] = date
        thumb = mapper.image_url(row.get("image_url") or row.get("thumbnail"))
        if thumb:
            md["thumb"] = thumb
        summary = next((overviews[l] for l in ctx.langs if overviews.get(l)), None) or row.get("overview")
        if summary:
            md["summary"] = summary
        return md

    async def _match_show(self, body: dict, ctx: Ctx, manual: bool, include_children: bool) -> list[dict]:
        title = str(body.get("title") or "")
        year = _int(body.get("year"))
        exact: list[int] = []
        guid_hint = keys.parse_guid(body.get("guid"))
        hints = ([guid_hint] if guid_hint else []) + keys.parse_tags(title) + keys.parse_tags(body.get("filename"))
        for hint in hints:
            sid = await self._series_from_hint(hint, "show")
            if sid and sid not in exact:
                exact.append(sid)
                if not manual:
                    break

        if exact and not manual:
            try:
                return [await self._scored_show(exact[0], 100, ctx, include_children)]
            except NotFound:
                exact = []

        found = [] if (exact and not manual) else await self._search(title, year, ctx)
        if not manual:
            if not found or found[0][1] < self.settings.match_min_score:
                return []
            sid, sc, _ = found[0]
            return [await self._scored_show(sid, sc, ctx, include_children)]

        results: list[dict] = []
        seen: set[int] = set()
        for sid in exact:
            try:
                results.append(await self._scored_show(sid, 100, ctx, False))
                seen.add(sid)
            except NotFound:
                continue
        for sid, sc, row in found:
            if sid in seen:
                continue
            seen.add(sid)
            results.append(self._search_result_md(row, sid, sc, ctx))
        return results[: self.settings.match_max_results]

    async def _scored_show(self, sid: int, score: int, ctx: Ctx, include_children: bool) -> dict:
        md = await self.show_item(sid, ctx, include_children)
        md["score"] = score
        return md

    async def _series_for_child(self, body: dict, ctx: Ctx, kind: str) -> int | None:
        title_field = "parentTitle" if kind == "season" else "grandparentTitle"
        show_guid_field = "parentGuid" if kind == "season" else "grandparentGuid"
        candidates: list[tuple[keys.IdHint, str]] = []
        show_hint = keys.parse_guid(body.get(show_guid_field))
        if show_hint:
            candidates.append((show_hint, "show"))
        own_hint = keys.parse_guid(body.get("guid"))
        if own_hint:
            candidates.append((own_hint, kind))
        for text in (body.get("filename"), body.get(title_field)):
            candidates.extend((h, "show") for h in keys.parse_tags(text))
        for hint, hint_kind in candidates:
            sid = await self._series_from_hint(hint, hint_kind)
            if sid:
                return sid
        title = str(body.get(title_field) or body.get("title") or "")
        found = await self._search(title, None, ctx)
        if found and found[0][1] >= self.settings.match_min_score:
            return found[0][0]
        return None

    async def _match_season(self, body: dict, ctx: Ctx, include_children: bool) -> list[dict]:
        sid = await self._series_for_child(body, ctx, "season")
        number = _int(body.get("index"))
        if sid is None or number is None:
            return []
        ctx.plex_order = orders.parse_order(body.get("episodeOrder")) or ctx.plex_order
        series = await self.series(sid, ctx)
        order, _, _ = self.resolve_order(series, ctx)
        try:
            return [await self.season_item(sid, order, number, ctx, include_children)]
        except NotFound:
            return []

    async def _match_episode(self, body: dict, ctx: Ctx, manual: bool) -> list[dict]:
        sid = await self._series_for_child(body, ctx, "episode")
        if sid is None:
            return []
        ctx.plex_order = orders.parse_order(body.get("episodeOrder")) or ctx.plex_order
        series, order, _, episodes, _ = await self._show_context(sid, ctx)
        season_no = _int(body.get("parentIndex"))
        number = _int(body.get("index"))
        date = mapper.iso_date(body.get("date"))
        guid_hint = keys.parse_guid(body.get("guid"))
        matches: list[dict] = []
        if guid_hint and guid_hint.source == "tvdb":
            matches = [e for e in episodes if str(e.get("id")) == guid_hint.value]
        if not matches and season_no is not None and number is not None:
            matches = [e for e in episodes if int(e["seasonNumber"]) == season_no and int(e["number"]) == number]
        if not matches and date:
            matches = [e for e in episodes if mapper.iso_date(e.get("aired")) == date]
            if season_no is not None and len(matches) > 1:
                matches = [e for e in matches if int(e["seasonNumber"]) == season_no] or matches
        if not matches:
            return []
        if not manual:
            matches = matches[:1]
        return [await self.episode_item(sid, order, int(e["id"]), ctx) for e in matches]


def _guid_map(md: dict) -> dict[str, str]:
    """``{"imdb": "tt123", "tvdb": "42"}`` from a Metadata ``Guid`` array."""
    out: dict[str, str] = {}
    for g in md.get("Guid") or []:
        scheme, _, value = str(g.get("id", "")).partition("://")
        if value and scheme not in out:
            out[scheme] = value
    return out


def _add_guid(md: dict, scheme: str, value: object) -> None:
    if value in (None, "", 0) or scheme in _guid_map(md):
        return
    md.setdefault("Guid", []).append({"id": f"{scheme}://{value}"})


def _first_url(images: list[dict] | None) -> str | None:
    return images[0]["url"] if images else None


def _prune(md: dict) -> dict:
    return {k: v for k, v in md.items() if v not in (None, "", [], {})}


def _flag(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
