"""Optional response customisation: includeFields / excludeFields /
includeElements / excludeElements (see "Response Customization" in the Plex
docs). These are treated as requests, not guarantees: identity fields are
always kept so Plex can still link items."""

from __future__ import annotations

from dataclasses import dataclass

ALWAYS_KEEP = {"ratingKey", "key", "guid", "type"}


def _split(value: object) -> set[str] | None:
    if value is None:
        return None
    items = {v.strip() for v in str(value).split(",") if v.strip()}
    return items or None


@dataclass
class Customization:
    include_fields: set[str] | None = None
    exclude_fields: set[str] | None = None
    include_elements: set[str] | None = None
    exclude_elements: set[str] | None = None

    @classmethod
    def from_params(cls, *sources: dict) -> "Customization":
        def pick(name: str) -> set[str] | None:
            for src in sources:
                if src and src.get(name) not in (None, ""):
                    return _split(src.get(name))
            return None

        return cls(
            include_fields=pick("includeFields"),
            exclude_fields=pick("excludeFields"),
            include_elements=pick("includeElements"),
            exclude_elements=pick("excludeElements"),
        )

    @property
    def active(self) -> bool:
        return any((self.include_fields, self.exclude_fields, self.include_elements, self.exclude_elements))

    def apply(self, item: dict) -> dict:
        if not self.active:
            return item
        out: dict = {}
        for key, value in item.items():
            is_element = isinstance(value, (list, dict))
            if is_element:
                if self.include_elements is not None and key not in self.include_elements:
                    continue
                if self.exclude_elements and key in self.exclude_elements:
                    continue
                if key == "Children" and isinstance(value, dict):
                    value = {**value, "Metadata": [self.apply(c) for c in value.get("Metadata") or []]}
            elif key not in ALWAYS_KEEP:
                if self.include_fields is not None and key not in self.include_fields:
                    continue
                if self.exclude_fields and key in self.exclude_fields:
                    continue
            out[key] = value
        return out
