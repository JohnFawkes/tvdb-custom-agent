"""Episode orderings (TVDB "season types") and how they map to Plex.

TheTVDB stores every alternative numbering of a show as a separate set of
seasons, each tagged with a *season type*. Plex calls these orderings
``SeasonType`` and passes the chosen one back to the provider as the
``episodeOrder`` parameter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Order:
    slug: str  # TVDB season-type "type" value, also used inside ratingKeys
    plex_id: str  # Plex SeasonType id (ASCII only)
    tag: str  # Short label, e.g. "DVD"
    title: str  # Label shown in the Plex UI


# Canonical TVDB season types, in the order they are listed to Plex.
ORDERS: tuple[Order, ...] = (
    Order("official", "tvdbAiring", "Aired", "TheTVDB (Aired)"),
    Order("dvd", "tvdbDvd", "DVD", "TheTVDB (DVD)"),
    Order("absolute", "tvdbAbsolute", "Absolute", "TheTVDB (Absolute)"),
    Order("alternate", "tvdbAlternate", "Alternate", "TheTVDB (Alternate)"),
    Order("regional", "tvdbRegional", "Regional", "TheTVDB (Regional)"),
    Order("altdvd", "tvdbAltdvd", "Alternate DVD", "TheTVDB (Alternate DVD)"),
    Order("alttwo", "tvdbAlttwo", "Alternate 2", "TheTVDB (Alternate 2)"),
)

BY_SLUG: dict[str, Order] = {o.slug: o for o in ORDERS}

# Fallback for TVDB numeric season-type ids (used for ``defaultSeasonType``)
# when a series record does not carry its own id -> type mapping.
TVDB_TYPE_IDS: dict[int, str] = {
    1: "official",
    2: "dvd",
    3: "absolute",
    4: "alternate",
    5: "regional",
    6: "altdvd",
    7: "alttwo",
}

DEFAULT = "default"  # "use whatever TVDB marks as the series' default order"

_ALIASES: dict[str, str] = {
    "default": DEFAULT,
    "series": DEFAULT,
    "official": "official",
    "aired": "official",
    "airing": "official",
    "air": "official",
    "broadcast": "official",
    "dvd": "dvd",
    "absolute": "absolute",
    "abs": "absolute",
    "alternate": "alternate",
    "alt": "alternate",
    "regional": "regional",
    "altdvd": "altdvd",
    "alternatedvd": "altdvd",
    "alttwo": "alttwo",
    "alt2": "alttwo",
    "alternate2": "alttwo",
}
for _o in ORDERS:
    _ALIASES[_o.plex_id.lower()] = _o.slug
    _ALIASES[_o.slug] = _o.slug
for _num, _slug in TVDB_TYPE_IDS.items():
    _ALIASES[str(_num)] = _slug


def parse_order(value: object) -> str | None:
    """Normalise a user/Plex supplied order name.

    Returns a TVDB slug (``official``, ``dvd`` ...), :data:`DEFAULT`, or
    ``None`` when the value is empty or not recognised (for example an
    ``episodeOrder`` that belongs to another provider such as ``tmdbAiring``).
    """
    if value is None:
        return None
    text = str(value).strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    if not text:
        return None
    if text.endswith("order") and text not in _ALIASES:
        text = text[: -len("order")]
    return _ALIASES.get(text)


def season_type_slug(season_type: dict | None) -> str | None:
    """Return the slug of a TVDB ``SeasonType`` object attached to a season."""
    if not season_type:
        return None
    slug = season_type.get("type")
    if slug:
        return str(slug)
    type_id = season_type.get("id")
    if isinstance(type_id, int):
        return TVDB_TYPE_IDS.get(type_id)
    return None


def series_default_order(series: dict) -> str:
    """Return the slug of the series' default season type."""
    default_id = series.get("defaultSeasonType")
    for st in series.get("seasonTypes") or []:
        if st.get("id") == default_id and st.get("type"):
            return str(st["type"])
    for season in series.get("seasons") or []:
        st = season.get("type") or {}
        if st.get("id") == default_id and st.get("type"):
            return str(st["type"])
    if isinstance(default_id, int) and default_id in TVDB_TYPE_IDS:
        return TVDB_TYPE_IDS[default_id]
    return "official"


def series_available_orders(series: dict) -> list[str]:
    """Orders that actually have seasons for this series, default first."""
    found: list[str] = []
    for season in series.get("seasons") or []:
        slug = season_type_slug(season.get("type"))
        if slug and slug not in found:
            found.append(slug)
    default = series_default_order(series)
    known = [o.slug for o in ORDERS]
    ordered = sorted(found, key=lambda s: known.index(s) if s in known else len(known))
    if default in ordered:
        ordered.remove(default)
    return [default, *ordered]


def season_type_names(series: dict) -> dict[str, str]:
    """Map slug -> descriptive TVDB name (includes ``alternateName``)."""
    names: dict[str, str] = {}
    candidates: Iterable[dict] = list(series.get("seasonTypes") or []) + [
        (s.get("type") or {}) for s in series.get("seasons") or []
    ]
    for st in candidates:
        slug = season_type_slug(st)
        if not slug or slug in names:
            continue
        name = st.get("name") or ""
        alt = st.get("alternateName") or ""
        if alt and alt.lower() not in name.lower():
            name = f"{name} ({alt})" if name else alt
        if name:
            names[slug] = name
    return names


def plex_season_types(series: dict, first: str | None = None) -> list[dict]:
    """Build the Plex ``SeasonType`` array for the orders this series has."""
    available = series_available_orders(series)
    if first and first in available:
        available.remove(first)
        available.insert(0, first)
    names = season_type_names(series)
    result = []
    for slug in available:
        order = BY_SLUG.get(slug)
        if order is None:
            continue
        title = order.title
        tvdb_name = names.get(slug)
        if tvdb_name and "(" in tvdb_name:
            # Surface alternate names such as "Alternate Order (Netflix)".
            title = f"TheTVDB ({tvdb_name})"
        result.append({"id": order.plex_id, "source": "tvdb", "tag": order.tag, "title": title})
    return result
