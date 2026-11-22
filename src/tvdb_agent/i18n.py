"""Language and country helpers.

Plex sends IETF language tags (``en-US``) and ISO 3166 alpha-2 countries
(``US``). TheTVDB uses its own mostly ISO 639-2 three-letter language ids
(``eng``) and ISO 3166 alpha-3 countries (``usa``).
"""

from __future__ import annotations

# ISO 639-1 -> TVDB language id.
_LANG2_TO_TVDB: dict[str, str] = {
    "ar": "ara", "bg": "bul", "bn": "ben", "ca": "cat", "cs": "ces", "cy": "cym",
    "da": "dan", "de": "deu", "el": "ell", "en": "eng", "es": "spa", "et": "est",
    "eu": "eus", "fa": "fas", "fi": "fin", "fr": "fra", "ga": "gle", "gl": "glg",
    "he": "heb", "hi": "hin", "hr": "hrv", "hu": "hun", "hy": "hye", "id": "ind",
    "is": "isl", "it": "ita", "ja": "jpn", "ka": "kat", "kk": "kaz", "ko": "kor",
    "lt": "lit", "lv": "lav", "mk": "mkd", "ml": "mal", "mr": "mar", "ms": "msa",
    "nb": "nor", "nl": "nld", "nn": "nor", "no": "nor", "pl": "pol", "pt": "por",
    "ro": "ron", "ru": "rus", "sk": "slk", "sl": "slv", "sq": "sqi", "sr": "srp",
    "sv": "swe", "sw": "swa", "ta": "tam", "te": "tel", "th": "tha", "tl": "tgl",
    "tr": "tur", "uk": "ukr", "ur": "urd", "vi": "vie", "zh": "zho",
}

# Region specific TVDB languages.
_REGIONAL: dict[str, str] = {
    "pt-br": "pt",  # TVDB: Portuguese - Brazil
    "zh-tw": "zhtw",
    "zh-hk": "yue",
    "zh-hant": "zhtw",
}

_TVDB_IDS = set(_LANG2_TO_TVDB.values()) | set(_REGIONAL.values())


def to_tvdb_language(value: str | None) -> str | None:
    """Convert ``en-US`` / ``en`` / ``eng`` to a TVDB language id."""
    if not value:
        return None
    text = value.strip().replace("_", "-").lower()
    if not text:
        return None
    if text in _REGIONAL:
        return _REGIONAL[text]
    if text in _TVDB_IDS:
        return text
    primary = text.split("-", 1)[0]
    if primary in _LANG2_TO_TVDB:
        return _LANG2_TO_TVDB[primary]
    if len(primary) == 3:
        return primary
    return None


# ISO 3166 alpha-3 -> (alpha-2, display name). Used when the TVDB /countries
# endpoint has not been loaded (or is unavailable).
COUNTRIES: dict[str, tuple[str, str]] = {
    "usa": ("us", "United States of America"),
    "gbr": ("gb", "United Kingdom"),
    "can": ("ca", "Canada"),
    "aus": ("au", "Australia"),
    "nzl": ("nz", "New Zealand"),
    "irl": ("ie", "Ireland"),
    "deu": ("de", "Germany"),
    "fra": ("fr", "France"),
    "esp": ("es", "Spain"),
    "ita": ("it", "Italy"),
    "nld": ("nl", "Netherlands"),
    "bel": ("be", "Belgium"),
    "che": ("ch", "Switzerland"),
    "aut": ("at", "Austria"),
    "swe": ("se", "Sweden"),
    "nor": ("no", "Norway"),
    "dnk": ("dk", "Denmark"),
    "fin": ("fi", "Finland"),
    "isl": ("is", "Iceland"),
    "pol": ("pl", "Poland"),
    "cze": ("cz", "Czech Republic"),
    "hun": ("hu", "Hungary"),
    "prt": ("pt", "Portugal"),
    "bra": ("br", "Brazil"),
    "mex": ("mx", "Mexico"),
    "arg": ("ar", "Argentina"),
    "chl": ("cl", "Chile"),
    "col": ("co", "Colombia"),
    "jpn": ("jp", "Japan"),
    "kor": ("kr", "South Korea"),
    "chn": ("cn", "China"),
    "twn": ("tw", "Taiwan"),
    "hkg": ("hk", "Hong Kong"),
    "ind": ("in", "India"),
    "tha": ("th", "Thailand"),
    "phl": ("ph", "Philippines"),
    "idn": ("id", "Indonesia"),
    "mys": ("my", "Malaysia"),
    "sgp": ("sg", "Singapore"),
    "rus": ("ru", "Russia"),
    "ukr": ("ua", "Ukraine"),
    "tur": ("tr", "Turkey"),
    "isr": ("il", "Israel"),
    "zaf": ("za", "South Africa"),
    "egy": ("eg", "Egypt"),
    "grc": ("gr", "Greece"),
    "rou": ("ro", "Romania"),
}


class CountryTable:
    """alpha-3 <-> alpha-2 lookups, seeded with :data:`COUNTRIES` and
    optionally extended from TVDB's ``/countries`` endpoint."""

    def __init__(self) -> None:
        self._by3: dict[str, tuple[str, str]] = dict(COUNTRIES)

    def load(self, records: list[dict]) -> None:
        for rec in records or []:
            code3 = str(rec.get("id") or "").lower()
            code2 = str(rec.get("shortCode") or "").lower()
            name = rec.get("name") or ""
            if code3 and code2:
                existing = self._by3.get(code3)
                self._by3[code3] = (code2, existing[1] if existing else name)

    def alpha2(self, code3: str | None) -> str | None:
        if not code3:
            return None
        hit = self._by3.get(code3.lower())
        return hit[0] if hit else None

    def alpha3(self, code2: str | None) -> str | None:
        if not code2:
            return None
        code2 = code2.lower()
        for code3, (c2, _) in self._by3.items():
            if c2 == code2:
                return code3
        return None

    def name(self, code3: str | None) -> str | None:
        if not code3:
            return None
        hit = self._by3.get(code3.lower())
        return hit[1] if hit else None
