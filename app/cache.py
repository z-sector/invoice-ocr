"""In-memory cache of successful parse results, keyed by the uploaded bytes.

Entries stay valid for PARSE_CACHE_TTL_HOURS (default 24, one day) from the moment
they were stored. The lifetime is absolute: reading an entry does not extend it.
Zero disables the cache. Entries live in this process and disappear on restart.

Reading a key drops that entry once it is older than the lifetime. The rest of the
cache is swept by purge_expired, which the API runs in a background task at most
once a minute and once at startup.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_PURGE_EVERY_SECONDS = 60.0
_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


def content_key(data: bytes) -> str:
    """Cache key for an upload: the sha256 of its bytes."""
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class CachedParse:
    invoice: dict
    sources: dict
    ocr: dict
    stored_at: float


class ParseCache:
    """Process-local map from the sha256 of an upload to its parse result."""

    def __init__(
        self,
        ttl_hours: float,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not math.isfinite(ttl_hours) or ttl_hours < 0:
            raise ValueError("ttl_hours must be a non-negative finite number of hours")
        self.ttl_hours = float(ttl_hours)
        self.ttl_seconds = self.ttl_hours * 3600.0
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._entries: dict[str, CachedParse] = {}
        self._next_purge = 0.0

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0

    def age_seconds(self, stored_at: float) -> int:
        return max(0, int(self._clock() - stored_at))

    def get(self, key: str) -> CachedParse | None:
        if not self.enabled:
            return None
        _require_key(key)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if self._expired(entry.stored_at):
                del self._entries[key]
                return None
            return _copy_entry(entry)

    def put(self, key: str, *, invoice: dict, sources: dict, ocr: dict) -> bool:
        """Store one successful parse. Returns False when the cache is off."""
        if not self.enabled:
            return False
        _require_key(key)
        entry = CachedParse(
            invoice=dict(invoice),
            sources=dict(sources),
            ocr=dict(ocr),
            stored_at=self._clock(),
        )
        with self._lock:
            self._entries[key] = entry
        return True

    def claim_purge(self) -> bool:
        """Reserve the once-a-minute sweep. The caller runs purge_expired afterwards."""
        if not self.enabled:
            return False
        now = self._clock()
        with self._lock:
            if now < self._next_purge:
                return False
            self._next_purge = now + _PURGE_EVERY_SECONDS
            return True

    def purge_expired(self) -> None:
        """Drop expired entries. Safe to run after the response is sent."""
        if not self.enabled:
            return
        with self._lock:
            expired = [key for key, entry in self._entries.items() if self._expired(entry.stored_at)]
            for key in expired:
                del self._entries[key]
            self._next_purge = self._clock() + _PURGE_EVERY_SECONDS

    def _expired(self, stored_at: float) -> bool:
        return self._clock() - stored_at >= self.ttl_seconds


def _require_key(key: str) -> None:
    if not _KEY_RE.fullmatch(key):
        raise ValueError("cache key must be a sha256 hex digest")


def _copy_entry(entry: CachedParse) -> CachedParse:
    return CachedParse(
        invoice=dict(entry.invoice),
        sources=dict(entry.sources),
        ocr=dict(entry.ocr),
        stored_at=entry.stored_at,
    )
