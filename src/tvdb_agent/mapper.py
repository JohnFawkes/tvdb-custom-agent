"""Pure helpers that turn TVDB records into pieces of Plex metadata."""

from __future__ import annotations

from typing import Iterable

from .i18n import CountryTable

ARTWORK_HOST = "https://artworks.thetvdb.com"

# TVDB artwork type id -> (record type, Plex image type). Used until (or if
# not) /artwork/types can be loaded. Plex image types: background,
# backgroundSquare, clearLogo, coverPoster, snapshot.
FALLBACK_ARTWORK_TYPES: dict[int, tuple[str, str | None]] = {
    1: ("series", None),  # banner
    2: ("series", "coverPoster"),
    3: ("series", "background"),
    5: ("series", None),  # icon
    6: ("season", None),  # banner
    7: ("season", "coverPoster"),
    8: ("season", "background"),
    10: ("season", None),  # icon
    11: ("episode", "snapshot"),
    12: ("episode", "snapshot"),
    22: ("series", None),  # clearart
    23: ("series", "clearLogo"),
}


def plex_image_type(name: str | None) -> str | None:
    n = (name or "").lower().replace("_", "").replace("-", "").replace(" ", "")
    if "clearlogo" in n:
        return "clearLogo"
    if "poster" in n:
        return "coverPoster"
    if "background" in n or "fanart" in n:
        return "background"
    if "screencap" in n:
        return "snapshot"
    return None


def build_artwork_type_map(records: list[dict] | None) -> dict[int, tuple[str, str | None]]:
    mapping = dict(FALLBACK_ARTWORK_TYPES)
    for rec in records or []:
        try:
            type_id = int(rec.get("id"))
        except (TypeError, ValueError):
            continue
        record_type = str(rec.get("recordType") or "").lower()
        if record_type not in {"series", "season", "episode"}:
            continue
        mapping[type_id] = (record_type, plex_image_type(rec.get("slug") or rec.get("name")))
    return mapping


def image_url(path: str | None) -> str | None:
    if not path:
        return None
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if path.startswith("/"):
        return ARTWORK_HOST + path
    return f"{ARTWORK_HOST}/{path}"


def rank_artworks(
    artworks: Iterable[dict],
    langs: list[str],
    original_language: str | None,
    textless_first: bool,
) -> list[dict]:
    """Sort artworks: preferred languages first (textless first for
    backgrounds/snapshots), then by TVDB score."""

    def bucket(art: dict) -> int:
        lang = art.get("language") or None
        if lang is None:
            return 0 if textless_first else len(langs) + 1
        if lang in langs:
            return langs.index(lang) + (1 if textless_first else 0)
        if lang == original_language:
            return len(langs) + 2
        return len(langs) + 3

    return sorted(artworks, key=lambda a: (bucket(a), -(a.get("score") or 0), a.get("id") or 0))


def select_images(
    artworks: Iterable[dict],
    type_map: dict[int, tuple[str, str | None]],
    record_type: str,
    langs: list[str],
    original_language: str | None,
    alt: str | None = None,
    per_type: int | None = 1,
) -> dict[str, list[dict]]:
    """Group artworks of ``record_type`` by Plex image type, ranked."""
    grouped: dict[str, list[dict]] = {}
    for art in artworks:
        info = type_map.get(art.get("type"))  # type: ignore[arg-type]
        if not info or info[0] != record_type or not info[1]:
            continue
        if not image_url(art.get("image")):
            continue
        grouped.setdefault(info[1], []).append(art)
    result: dict[str, list[dict]] = {}
    for plex_type, arts in grouped.items():
        ranked = rank_artworks(arts, langs, original_language, plex_type in ("background", "snapshot"))
        if per_type:
            ranked = ranked[:per_type]
        result[plex_type] = [_image(plex_type, a.get("image"), alt) for a in ranked]
    return result


def original_images(
    artworks: Iterable[dict],
    type_map: dict[int, tuple[str, str | None]],
    record_type: str,
    original_language: str | None,
    alt: str | None = None,
) -> list[dict]:
    if not original_language:
        return []
    arts = [a for a in artworks if a.get("language") == original_language]
    grouped = select_images(arts, type_map, record_type, [original_language], original_language, alt)
    return [img for imgs in grouped.values() for img in imgs]


def _image(plex_type: str, url: str | None, alt: str | None) -> dict:
    img = {"type": plex_type, "url": image_url(url)}
    if alt:
        img["alt"] = alt
    return img


IMAGE_TYPE_ORDER = ["coverPoster", "background", "backgroundSquare", "clearLogo", "snapshot"]


def flatten_images(grouped: dict[str, list[dict]]) -> list[dict]:
    out: list[dict] = []
    for plex_type in IMAGE_TYPE_ORDER:
        out.extend(grouped.get(plex_type, []))
    return out


# ------------------------------------------------------------ translations
def translation(record: dict, field: str, langs: list[str]) -> str | None:
    """Pick ``name``/``overview`` from a record's ``translations`` block."""
    block = record.get("translations") or {}
    key = "nameTranslations" if field == "name" else "overviewTranslations"
    entries = block.get(key) or []
    for lang in langs:
        for entry in entries:
            if entry.get("language") != lang:
                continue
            if field == "name" and entry.get("isAlias"):
                continue
            value = entry.get(field)
            if value:
                return value.strip()
    return None


# ------------------------------------------------------------------ people
_PEOPLE_TYPE_IDS = {
    1: "director", 2: "writer", 3: "actor", 4: "guest star", 5: "crew", 6: "creator",
    7: "producer", 8: "showrunner", 9: "musical guest", 10: "host", 11: "executive producer",
}
_PEOPLE_ELEMENT = {
    "actor": "Role",
    "guest star": "Role",
    "host": "Role",
    "musical guest": "Role",
    "director": "Director",
    "writer": "Writer",
    "creator": "Writer",
    "producer": "Producer",
    "executive producer": "Producer",
    "showrunner": "Producer",
}


def people(characters: Iterable[dict], max_cast: int) -> dict[str, list[dict]]:
    """Split TVDB characters into Plex Role/Director/Writer/Producer arrays."""
    out: dict[str, list[dict]] = {}
    seen: set[tuple[str, str, str]] = set()

    def sort_key(c: dict) -> tuple:
        sort = c.get("sort") or 0
        return (0 if c.get("isFeatured") else 1, sort if sort > 0 else 10_000, c.get("id") or 0)

    for char in sorted(characters or [], key=sort_key):
        kind = (char.get("peopleType") or _PEOPLE_TYPE_IDS.get(char.get("type"), "") or "").lower()
        element = _PEOPLE_ELEMENT.get(kind)
        person = (char.get("personName") or "").strip()
        if not element or not person:
            continue
        if element == "Role":
            role = (char.get("name") or "").strip()
        else:
            role = kind.title()
        dedupe = (element, person, role)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        entries = out.setdefault(element, [])
        if element == "Role" and len(entries) >= max_cast:
            continue
        entry: dict = {"tag": person}
        if role:
            entry["role"] = role
        thumb = image_url(char.get("personImgURL") or char.get("image"))
        if thumb:
            entry["thumb"] = thumb
        entries.append(entry)
    for element, entries in out.items():
        for i, entry in enumerate(entries, start=1):
            entry["order"] = i
    return out


# ------------------------------------------------------------- misc fields
def external_guids(remote_ids: Iterable[dict] | None) -> list[dict]:
    guids: list[dict] = []
    seen: set[str] = set()
    for rid in remote_ids or []:
        value = str(rid.get("id") or "").strip()
        source = (rid.get("sourceName") or "").lower().replace(" ", "")
        if not value:
            continue
        if "imdb" in source and value.startswith("tt"):
            gid = f"imdb://{value}"
        elif "themoviedb" in source or source == "tmdb":
            gid = f"tmdb://{value}"
        else:
            continue
        if gid not in seen:
            seen.add(gid)
            guids.append({"id": gid})
    return guids


def content_rating(ratings: Iterable[dict] | None, country2: str | None, countries: CountryTable) -> str | None:
    ratings = [r for r in ratings or [] if r.get("name")]
    if not ratings:
        return None
    wanted = countries.alpha3(country2) if country2 else None
    by_country = {str(r.get("country") or "").lower(): r for r in ratings}
    for code3 in (wanted, "usa"):
        if code3 and code3 in by_country:
            return _format_rating(by_country[code3], countries)
    return _format_rating(ratings[0], countries)


def _format_rating(rating: dict, countries: CountryTable) -> str:
    code3 = str(rating.get("country") or "").lower()
    name = str(rating["name"]).strip()
    if code3 in ("", "usa"):
        return name
    code2 = countries.alpha2(code3)
    return f"{code2}/{name}" if code2 else name


def companies_of(companies: Iterable[dict] | None, *type_names: str) -> list[str]:
    wanted = {t.lower() for t in type_names}
    names: list[str] = []
    for comp in companies or []:
        ctype = comp.get("companyType") or {}
        tname = str(ctype.get("companyTypeName") or "").lower()
        if tname in wanted and comp.get("name") and comp["name"] not in names:
            names.append(comp["name"])
    return names


def tags(values: Iterable[str]) -> list[dict]:
    out, seen = [], set()
    for v in values:
        if v and v not in seen:
            seen.add(v)
            out.append({"tag": v})
    return out


def minutes_to_ms(minutes: object) -> int | None:
    try:
        value = int(minutes)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value * 60_000 if value > 0 else None


def iso_date(value: object) -> str | None:
    if not value:
        return None
    text = str(value)[:10]
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text
    return None
