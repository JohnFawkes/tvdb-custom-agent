"""Per-show episode-order overrides.

Two sources are combined:

* ``show_overrides`` in the YAML config (global and per profile), read-only.
* A JSON file in the data directory, editable at runtime through the admin
  API / web page.

Lookup order for a show: runtime(profile) > config(profile) >
runtime(global) > config(global).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from . import orders
from .config import Settings

log = logging.getLogger(__name__)

GLOBAL = "*"


class OverrideStoreError(RuntimeError):
    pass


class OverrideStore:
    def __init__(self, settings: Settings, path: str | os.PathLike | None = None) -> None:
        self.settings = settings
        self.path = Path(path) if path else Path(settings.data_dir) / "overrides.json"
        self._lock = threading.Lock()
        self._data: dict[str, dict[int, str]] = {}
        self._load()

    # ------------------------------------------------------------ storage
    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Could not read %s: %s", self.path, exc)
            return
        for scope, entries in (raw or {}).items():
            parsed: dict[int, str] = {}
            for key, value in (entries or {}).items():
                order = orders.parse_order(value)
                try:
                    parsed[int(key)] = order  # type: ignore[assignment]
                except ValueError:
                    continue
                if order is None:
                    parsed.pop(int(key), None)
            self._data[scope] = parsed

    def _save(self) -> None:
        try:
            self._write()
        except OSError as exc:
            raise OverrideStoreError(
                f"could not write {self.path}: {exc.strerror or exc}. "
                "Make sure the data directory is writable by the agent's user."
            ) from exc

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            scope: {str(k): v for k, v in sorted(entries.items())}
            for scope, entries in sorted(self._data.items())
            if entries
        }
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".overrides.", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------- lookup
    def lookup(self, profile_name: str | None, series_id: int) -> tuple[str | None, str | None]:
        """Return ``(order, source)`` for a show, or ``(None, None)``."""
        prof = self.settings.profile(profile_name)
        if profile_name:
            hit = self._data.get(profile_name, {}).get(series_id)
            if hit:
                return hit, f"runtime:{profile_name}"
            if prof and series_id in prof.show_overrides:
                return prof.show_overrides[series_id], f"config:{profile_name}"
        hit = self._data.get(GLOBAL, {}).get(series_id)
        if hit:
            return hit, "runtime:global"
        if series_id in self.settings.show_overrides:
            return self.settings.show_overrides[series_id], "config:global"
        return None, None

    # ------------------------------------------------------------ mutation
    def set(self, scope: str, series_id: int, order: str) -> str:
        parsed = orders.parse_order(order)
        if parsed is None:
            raise ValueError(f"unknown episode order '{order}'")
        with self._lock:
            entries = self._data.setdefault(scope, {})
            previous = entries.get(int(series_id))
            entries[int(series_id)] = parsed
            try:
                self._save()
            except OverrideStoreError:
                if previous is None:
                    entries.pop(int(series_id), None)
                else:
                    entries[int(series_id)] = previous
                raise
        return parsed

    def delete(self, scope: str, series_id: int) -> bool:
        with self._lock:
            entries = self._data.get(scope, {})
            previous = entries.pop(int(series_id), None)
            if previous is not None:
                try:
                    self._save()
                except OverrideStoreError:
                    entries[int(series_id)] = previous
                    raise
        return previous is not None

    def listing(self) -> list[dict]:
        rows: list[dict] = []
        for scope, entries in self._data.items():
            for sid, order in entries.items():
                rows.append({"scope": scope, "seriesId": sid, "order": order, "source": "runtime"})
        for sid, order in self.settings.show_overrides.items():
            rows.append({"scope": GLOBAL, "seriesId": sid, "order": order, "source": "config"})
        for prof in self.settings.profiles.values():
            for sid, order in prof.show_overrides.items():
                rows.append({"scope": prof.name, "seriesId": sid, "order": order, "source": "config"})
        rows.sort(key=lambda r: (r["scope"] != GLOBAL, str(r["scope"]), r["seriesId"]))
        return rows
