"""Configuration: environment variables plus an optional YAML file.

Precedence (highest first): environment variables, YAML file, defaults.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import orders
from .i18n import to_tvdb_language

log = logging.getLogger(__name__)

IDENTIFIER_PREFIX = "tv.plex.agents.custom."
DEFAULT_IDENTIFIER = IDENTIFIER_PREFIX + "tvdbcustomagent"
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")
IDENTIFIER_RE = re.compile(r"^tv\.plex\.agents\.custom\.[A-Za-z0-9.]+$")

# Names accepted for the global (all profiles) override scope.
GLOBAL_SCOPE_NAMES = {"*", "global", "all", "_", "root"}

# Profiles that exist without any configuration: one per TVDB ordering.
BUILTIN_PROFILE_ORDERS = {
    "aired": "official",
    "dvd": "dvd",
    "absolute": "absolute",
    "alternate": "alternate",
    "regional": "regional",
    "altdvd": "altdvd",
    "alttwo": "alttwo",
}


class ConfigError(ValueError):
    pass


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_overrides(raw: Any, where: str) -> dict[int, str]:
    result: dict[int, str] = {}
    if not raw:
        return result
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: show_overrides must be a mapping of TVDB id -> order")
    for key, value in raw.items():
        try:
            series_id = int(str(key).strip())
        except ValueError as exc:
            raise ConfigError(f"{where}: '{key}' is not a TVDB series id") from exc
        order = orders.parse_order(value)
        if order is None:
            raise ConfigError(f"{where}: unknown episode order '{value}' for series {series_id}")
        result[series_id] = order
    return result


@dataclass
class Profile:
    """A provider "personality". Each profile is a separate Plex provider
    (its own URL and identifier) so each Plex library can use a different
    one, e.g. a DVD-ordered library and an absolute-ordered anime library."""

    name: str | None  # None = the root provider
    identifier: str
    title: str
    episode_order: str = orders.DEFAULT
    language: str | None = None  # TVDB language id used when Plex sends none
    force_language: bool = False  # ignore X-Plex-Language
    honor_plex_episode_order: bool = True
    fallback_to_default_order: bool = True
    show_overrides: dict[int, str] = field(default_factory=dict)
    builtin: bool = False

    @property
    def base_path(self) -> str:
        return f"/profiles/{self.name}" if self.name else ""


@dataclass
class Settings:
    tvdb_api_key: str = ""
    tvdb_pin: str | None = None
    tvdb_base_url: str = "https://api4.thetvdb.com/v4"

    identifier: str = DEFAULT_IDENTIFIER
    title: str = "TheTVDB (self-hosted)"
    language: str = "eng"
    fallback_languages: list[str] = field(default_factory=lambda: ["eng"])
    episode_order: str = orders.DEFAULT
    honor_plex_episode_order: bool = True
    fallback_to_default_order: bool = True
    show_overrides: dict[int, str] = field(default_factory=dict)

    theme_music: bool = True
    season_titles: bool = True  # use TVDB season names when present
    episode_details: bool = True  # cast/crew/IMDb ids for every episode of a season
    max_cast: int = 50
    match_min_score: int = 40
    match_max_results: int = 10
    enable_builtin_profiles: bool = True

    cache_ttl: int = 12 * 3600
    cache_max_entries: int = 4096
    max_concurrency: int = 8

    admin_token: str | None = None
    data_dir: str = "./data"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    public_url: str | None = None  # how Plex/browsers reach this agent (admin page, Plex registration)
    plex_url: str | None = None  # Plex Media Server URL, for "Register in Plex"
    plex_token: str | None = None

    profiles: dict[str, Profile] = field(default_factory=dict)
    root: Profile | None = None

    # ------------------------------------------------------------------
    def profile(self, name: str | None) -> Profile | None:
        if not name:
            return self.root
        return self.profiles.get(name) or self.profiles.get(name.lower())

    def all_profiles(self) -> list[Profile]:
        return [p for p in [self.root, *self.profiles.values()] if p is not None]


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else None


def load_settings(config_file: str | os.PathLike | None = None, env: bool = True) -> Settings:
    data: dict[str, Any] = {}
    path = config_file or (_env("CONFIG_FILE") if env else None)
    if path is None:
        for candidate in ("/config/config.yaml", "./config.yaml"):
            if Path(candidate).is_file():
                path = candidate
                break
    if path:
        p = Path(path)
        if p.is_file():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            log.info("Loaded configuration from %s", p)
        elif config_file is not None or (env and _env("CONFIG_FILE")):
            raise ConfigError(f"Config file {p} does not exist")
    return build_settings(data, env=env)


def build_settings(data: dict[str, Any], env: bool = True) -> Settings:
    s = Settings()
    tvdb = data.get("tvdb") or {}
    provider = data.get("provider") or {}
    defaults = data.get("defaults") or {}
    server = data.get("server") or {}
    cache = data.get("cache") or {}

    s.tvdb_api_key = str(tvdb.get("api_key") or "")
    s.tvdb_pin = tvdb.get("pin") or None
    s.tvdb_base_url = tvdb.get("base_url") or s.tvdb_base_url
    s.identifier = provider.get("identifier") or s.identifier
    s.title = provider.get("title") or s.title
    s.enable_builtin_profiles = _bool(provider.get("builtin_profiles"), True)

    s.language = defaults.get("language") or s.language
    if defaults.get("fallback_languages") is not None:
        s.fallback_languages = list(defaults.get("fallback_languages") or [])
    s.episode_order = str(defaults.get("episode_order") or s.episode_order)
    s.honor_plex_episode_order = _bool(defaults.get("honor_plex_episode_order"), True)
    s.fallback_to_default_order = _bool(defaults.get("fallback_to_default_order"), True)
    s.theme_music = _bool(defaults.get("theme_music"), True)
    s.season_titles = _bool(defaults.get("season_titles"), True)
    s.episode_details = _bool(defaults.get("episode_details"), True)
    s.max_cast = int(defaults.get("max_cast") or s.max_cast)
    s.match_min_score = int(defaults.get("match_min_score", s.match_min_score))
    s.match_max_results = int(defaults.get("match_max_results") or s.match_max_results)

    s.cache_ttl = int(cache.get("ttl") or s.cache_ttl)
    s.cache_max_entries = int(cache.get("max_entries") or s.cache_max_entries)
    s.max_concurrency = int(tvdb.get("max_concurrency") or s.max_concurrency)

    s.admin_token = server.get("admin_token") or None
    s.data_dir = server.get("data_dir") or s.data_dir
    s.host = server.get("host") or s.host
    s.port = int(server.get("port") or s.port)
    s.log_level = server.get("log_level") or s.log_level
    s.public_url = server.get("public_url") or None
    plex = data.get("plex") or {}
    s.plex_url = plex.get("url") or None
    s.plex_token = plex.get("token") or None

    if env:
        s.tvdb_api_key = _env("TVDB_API_KEY") or s.tvdb_api_key
        s.tvdb_pin = _env("TVDB_PIN") or s.tvdb_pin
        s.tvdb_base_url = _env("TVDB_BASE_URL") or s.tvdb_base_url
        s.identifier = _env("PROVIDER_IDENTIFIER") or s.identifier
        s.title = _env("PROVIDER_TITLE") or s.title
        s.language = _env("DEFAULT_LANGUAGE") or s.language
        s.episode_order = _env("DEFAULT_EPISODE_ORDER") or s.episode_order
        if _env("HONOR_PLEX_EPISODE_ORDER") is not None:
            s.honor_plex_episode_order = _bool(_env("HONOR_PLEX_EPISODE_ORDER"), True)
        if _env("EPISODE_DETAILS") is not None:
            s.episode_details = _bool(_env("EPISODE_DETAILS"), True)
        if _env("THEME_MUSIC") is not None:
            s.theme_music = _bool(_env("THEME_MUSIC"), True)
        if _env("BUILTIN_PROFILES") is not None:
            s.enable_builtin_profiles = _bool(_env("BUILTIN_PROFILES"), True)
        s.admin_token = _env("ADMIN_TOKEN") or s.admin_token
        s.data_dir = _env("DATA_DIR") or s.data_dir
        s.host = _env("HOST") or s.host
        s.port = int(_env("PORT") or s.port)
        s.log_level = _env("LOG_LEVEL") or s.log_level
        s.public_url = _env("PUBLIC_URL") or s.public_url
        s.plex_url = _env("PLEX_URL") or s.plex_url
        s.plex_token = _env("PLEX_TOKEN") or s.plex_token
        if _env("CACHE_TTL"):
            s.cache_ttl = int(_env("CACHE_TTL"))  # type: ignore[arg-type]

    # ---------------------------------------------------------- validate
    if not IDENTIFIER_RE.match(s.identifier):
        raise ConfigError(
            f"provider identifier '{s.identifier}' must start with '{IDENTIFIER_PREFIX}' "
            "and contain only ASCII letters, digits and periods"
        )
    lang = to_tvdb_language(s.language)
    if not lang:
        raise ConfigError(f"unknown default language '{s.language}'")
    s.language = lang
    s.fallback_languages = [l for l in (to_tvdb_language(x) for x in s.fallback_languages) if l]
    order = orders.parse_order(s.episode_order)
    if order is None:
        raise ConfigError(f"unknown default episode_order '{s.episode_order}'")
    s.episode_order = order
    s.show_overrides = _parse_overrides(data.get("show_overrides"), "show_overrides")

    s.root = Profile(
        name=None,
        identifier=s.identifier,
        title=s.title,
        episode_order=s.episode_order,
        language=s.language,
        honor_plex_episode_order=s.honor_plex_episode_order,
        fallback_to_default_order=s.fallback_to_default_order,
    )

    if s.enable_builtin_profiles:
        for name, slug in BUILTIN_PROFILE_ORDERS.items():
            o = orders.BY_SLUG[slug]
            s.profiles[name] = Profile(
                name=name,
                identifier=f"{s.identifier}.{name}",
                title=f"{s.title} - {o.tag} order",
                episode_order=slug,
                language=s.language,
                honor_plex_episode_order=s.honor_plex_episode_order,
                fallback_to_default_order=s.fallback_to_default_order,
                builtin=True,
            )

    for name, raw in (data.get("profiles") or {}).items():
        name = str(name)
        raw = raw or {}
        if not PROFILE_NAME_RE.match(name):
            raise ConfigError(f"profile name '{name}' may only contain ASCII letters and digits")
        if name.lower() in GLOBAL_SCOPE_NAMES:
            raise ConfigError(f"profile name '{name}' is reserved")
        p_order = orders.parse_order(raw.get("episode_order", orders.DEFAULT))
        if p_order is None:
            raise ConfigError(f"profile '{name}': unknown episode_order '{raw.get('episode_order')}'")
        p_lang = to_tvdb_language(raw.get("language")) if raw.get("language") else s.language
        if raw.get("language") and not p_lang:
            raise ConfigError(f"profile '{name}': unknown language '{raw.get('language')}'")
        identifier = raw.get("identifier") or f"{s.identifier}.{name}"
        if not IDENTIFIER_RE.match(identifier):
            raise ConfigError(f"profile '{name}': invalid identifier '{identifier}'")
        s.profiles[name] = Profile(
            name=name,
            identifier=identifier,
            title=raw.get("title") or f"{s.title} - {name}",
            episode_order=p_order,
            language=p_lang,
            force_language=_bool(raw.get("force_language"), False),
            honor_plex_episode_order=_bool(raw.get("honor_plex_episode_order"), s.honor_plex_episode_order),
            fallback_to_default_order=_bool(raw.get("fallback_to_default_order"), s.fallback_to_default_order),
            show_overrides=_parse_overrides(raw.get("show_overrides"), f"profile '{name}'"),
        )

    idents = [p.identifier for p in s.all_profiles()]
    dupes = {i for i in idents if idents.count(i) > 1}
    if dupes:
        raise ConfigError(f"duplicate provider identifiers: {', '.join(sorted(dupes))}")
    return s
