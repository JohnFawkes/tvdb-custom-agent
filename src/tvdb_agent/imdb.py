"""IMDb ratings from IMDb's public dataset (no API key).

IMDb publishes ``title.ratings.tsv.gz`` daily at https://datasets.imdbws.com/
(for personal, non-commercial use; see
https://developer.imdb.com/non-commercial-datasets/). It holds the rating of
every title, episodes included: about 1.7 million rows, ~9 MB compressed.

The file is cached in ``DATA_DIR/imdb/`` and refreshed with a conditional
download (ETag / Last-Modified). It is loaded into one sorted ``array('Q')``
of ``(tconst number << 8) | rating*10`` keys: ~14 MB of memory and a binary
search per lookup.
"""

from __future__ import annotations

import asyncio
import gzip
import heapq
import io
import json
import logging
import os
import tempfile
import time
from array import array
from bisect import bisect_left
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

DATASET_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"


def parse_ratings(stream: io.BufferedIOBase | gzip.GzipFile) -> array:
    """Parse ``title.ratings.tsv`` (already decompressed stream of bytes)."""
    groups: dict[int, array] = {}
    reader = io.TextIOWrapper(stream, encoding="utf-8")
    header = next(reader, "")
    if not header.startswith("tconst"):
        raise ValueError("not an IMDb title.ratings.tsv file")
    for line in reader:
        try:
            tconst, rating, _ = line.split("\t", 2)
            digits = tconst[2:]
            key = (int(digits) << 8) | int(round(float(rating) * 10))
        except ValueError:
            continue
        group = groups.get(len(digits))
        if group is None:
            group = groups[len(digits)] = array("Q")
        group.append(key)
    # The file is ordered by tconst as text, so ids of equal length are already
    # in numeric order; merge the groups instead of sorting everything (keeps
    # peak memory low). Sort any group that is not, just in case.
    for length, group in groups.items():
        if any(group[i] > group[i + 1] for i in range(len(group) - 1)):
            groups[length] = array("Q", sorted(group))
    return array("Q", heapq.merge(*groups.values()))


class IMDbRatings:
    def __init__(
        self,
        data_dir: str | os.PathLike,
        *,
        url: str = DATASET_URL,
        refresh_hours: float = 24,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.url = url
        self.refresh_seconds = max(1.0, refresh_hours) * 3600
        self.dir = Path(data_dir) / "imdb"
        self.path = self.dir / "title.ratings.tsv.gz"
        self.meta_path = self.dir / "title.ratings.json"
        self._keys = array("Q")
        self.loaded_at: float | None = None
        self.source_modified: str | None = None
        self._task: asyncio.Task | None = None
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=True,
                                       headers={"User-Agent": "tvdb-custom-agent"})

    # ------------------------------------------------------------- lookups
    @property
    def loaded(self) -> bool:
        return len(self._keys) > 0

    def __len__(self) -> int:
        return len(self._keys)

    def rating(self, imdb_id: str | None) -> float | None:
        if not imdb_id or not imdb_id.startswith("tt"):
            return None
        try:
            number = int(imdb_id[2:])
        except ValueError:
            return None
        keys = self._keys
        i = bisect_left(keys, number << 8)
        if i < len(keys) and keys[i] >> 8 == number:
            value = keys[i] & 0xFF
            return value / 10 if value else None
        return None

    # ------------------------------------------------------------ loading
    def _read_meta(self) -> dict:
        try:
            return json.loads(self.meta_path.read_text())
        except (OSError, ValueError):
            return {}

    def _load_file(self, path: Path) -> None:
        with gzip.open(path, "rb") as fh:
            keys = parse_ratings(fh)
        self._keys = keys
        self.loaded_at = time.time()
        self.source_modified = self._read_meta().get("last_modified")
        log.info("Loaded %d IMDb ratings (dataset %s)", len(keys), self.source_modified or "unknown date")

    async def refresh(self, force: bool = False) -> bool:
        """Download the dataset if it changed, then (re)load it. Returns True
        when new data was loaded."""
        meta = {} if force else self._read_meta()
        headers = {}
        if self.path.is_file() and not force:
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]
        resp = await self._http.get(self.url, headers=headers)
        if resp.status_code == 304:
            log.debug("IMDb dataset unchanged")
            if not self.loaded and self.path.is_file():
                await asyncio.to_thread(self._load_file, self.path)
                return True
            return False
        resp.raise_for_status()
        content = resp.content
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            os.replace(tmp, self.path)
            self.meta_path.write_text(json.dumps({
                "etag": resp.headers.get("etag"),
                "last_modified": resp.headers.get("last-modified"),
                "downloaded_at": time.time(),
            }))
            await asyncio.to_thread(self._load_file, self.path)
        except OSError as exc:
            log.warning("Could not cache the IMDb dataset in %s (%s); keeping it in memory only", self.dir, exc)

            def load_bytes() -> None:
                with gzip.open(io.BytesIO(content), "rb") as fh:
                    self._keys = parse_ratings(fh)
                self.loaded_at = time.time()
                self.source_modified = resp.headers.get("last-modified")

            await asyncio.to_thread(load_bytes)
        return True

    async def _run(self) -> None:
        if self.path.is_file():
            try:
                await asyncio.to_thread(self._load_file, self.path)
            except (OSError, ValueError, EOFError) as exc:
                log.warning("Cached IMDb dataset unreadable (%s); downloading it again", exc)
        while True:
            meta = self._read_meta()
            age = time.time() - float(meta.get("downloaded_at") or 0)
            if not self.loaded or age >= self.refresh_seconds:
                try:
                    await self.refresh()
                except (httpx.HTTPError, OSError, ValueError, EOFError) as exc:
                    log.warning("IMDb ratings dataset download failed: %s", exc)
                    await asyncio.sleep(min(self.refresh_seconds, 1800))
                    continue
                age = 0
            await asyncio.sleep(max(60.0, self.refresh_seconds - age))

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="imdb-ratings")

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._http.aclose()

    def status(self) -> dict:
        return {"loaded": self.loaded, "titles": len(self._keys), "datasetDate": self.source_modified}
