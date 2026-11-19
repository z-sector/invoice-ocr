"""Local disk cache of successful parse results, keyed by the uploaded bytes.

Entries stay valid for PARSE_CACHE_TTL_HOURS (default 24, one day) from the moment
they were stored. The lifetime is absolute: reading an entry does not extend it.
Zero disables the cache. The directory is PARSE_CACHE_DIR, or ~/.cache/invoice-ocr.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import (
    MAX_IMAGE_SIDE,
    MAX_PDF_PAGES,
    OCR_DEVICE,
    OCR_LANG,
    PDF_RENDER_DPI,
    USE_DOC_ORIENTATION_CLASSIFY,
    USE_DOC_UNWARPING,
    USE_TEXTLINE_ORIENTATION,
)

logger = logging.getLogger(__name__)

# Bump when the cached JSON shape changes.
CACHE_SCHEMA = 1
_PURGE_EVERY_SECONDS = 60.0
_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT: str | None = None


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while storing a parse cache entry")
        view = view[written:]


def content_key(data: bytes) -> str:
    """Cache key for an upload: the sha256 of its bytes."""
    return hashlib.sha256(data).hexdigest()


def settings_fingerprint() -> str:
    """Identity of the code and settings that can change a parse of the same bytes."""
    global _FINGERPRINT
    if _FINGERPRINT is None:
        _FINGERPRINT = _compute_fingerprint()
    return _FINGERPRINT


def _compute_fingerprint() -> str:
    code = hashlib.sha256()
    for name in ("parser.py", "ocr.py"):
        code.update(name.encode())
        code.update(b"\0")
        code.update(Path(__file__).with_name(name).read_bytes())
    payload = {
        "schema": CACHE_SCHEMA,
        "lang": OCR_LANG,
        "device": OCR_DEVICE,
        "doc_orientation": USE_DOC_ORIENTATION_CLASSIFY,
        "unwarp": USE_DOC_UNWARPING,
        "textline_orientation": USE_TEXTLINE_ORIENTATION,
        "max_image_side": MAX_IMAGE_SIDE,
        "pdf_dpi": PDF_RENDER_DPI,
        "max_pdf_pages": MAX_PDF_PAGES,
        "code": code.hexdigest(),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class CachedParse:
    invoice: dict
    sources: dict
    ocr: dict
    stored_at: float


class ParseCache:
    """JSON files in one directory. The file name is the sha256 of the upload."""

    def __init__(
        self,
        directory: Path | str,
        ttl_hours: float,
        *,
        fingerprint: str | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not math.isfinite(ttl_hours) or ttl_hours < 0:
            raise ValueError("ttl_hours must be a non-negative finite number of hours")
        self.directory = Path(directory).expanduser()
        self.ttl_hours = float(ttl_hours)
        self.ttl_seconds = self.ttl_hours * 3600.0
        self.fingerprint = settings_fingerprint() if fingerprint is None else fingerprint
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._next_purge = 0.0

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0

    def age_seconds(self, stored_at: float) -> int:
        return max(0, int(self._clock() - stored_at))

    def get(self, key: str) -> CachedParse | None:
        if not self.enabled:
            return None
        path = self._path(key)
        with self._lock:
            self._maybe_purge_unlocked()
            return self._read_unlocked(path)

    def put(self, key: str, *, invoice: dict, sources: dict, ocr: dict) -> bool:
        """Store one successful parse. Returns False when the cache is off or the write failed."""
        if not self.enabled:
            return False
        path = self._path(key)
        with self._lock:
            payload = {
                "schema": CACHE_SCHEMA,
                "fingerprint": self.fingerprint,
                "stored_at": self._clock(),
                "invoice": invoice,
                "sources": sources,
                "ocr": ocr,
            }
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            try:
                self._ensure_dir()
                self._write_atomic(path, data)
                self._maybe_purge_unlocked()
            except OSError:
                logger.warning("Could not store parse cache entry %s", path.name, exc_info=True)
                return False
        return True

    def purge_expired(self) -> None:
        if not self.enabled or not self.directory.exists():
            return
        with self._lock:
            self._purge_expired_unlocked()
            self._next_purge = self._clock() + _PURGE_EVERY_SECONDS

    def _path(self, key: str) -> Path:
        if not _KEY_RE.fullmatch(key):
            raise ValueError("cache key must be a sha256 hex digest")
        return self.directory / f"{key}.json"

    def _expired(self, stored_at: float) -> bool:
        return self._clock() - stored_at >= self.ttl_seconds

    def _ensure_dir(self) -> None:
        if self.directory.is_dir():
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            logger.debug("Could not restrict parse cache directory permissions", exc_info=True)

    def _write_atomic(self, path: Path, data: bytes) -> None:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    logger.debug("Could not restrict parse cache file permissions", exc_info=True)
                _write_all(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                logger.debug("Could not restrict parse cache file permissions", exc_info=True)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _maybe_purge_unlocked(self) -> None:
        now = self._clock()
        if now < self._next_purge:
            return
        try:
            self._purge_expired_unlocked()
        except OSError:
            logger.warning("Could not purge the parse cache", exc_info=True)
        self._next_purge = now + _PURGE_EVERY_SECONDS

    def _purge_expired_unlocked(self) -> None:
        if not self.directory.is_dir():
            return
        for path in list(self.directory.glob("*.json")):
            if self._file_is_stale(path):
                self._discard(path)
        for path in list(self.directory.glob(".*.tmp")):
            try:
                age = self._clock() - path.stat().st_mtime
            except OSError:
                continue
            if age >= self.ttl_seconds:
                self._discard(path)

    def _file_is_stale(self, path: Path) -> bool:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return True
            stored_at = float(payload["stored_at"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return True
        return (
            payload.get("schema") != CACHE_SCHEMA
            or payload.get("fingerprint") != self.fingerprint
            or not math.isfinite(stored_at)
            or self._expired(stored_at)
        )

    def _read_unlocked(self, path: Path) -> CachedParse | None:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            logger.warning("Could not read parse cache entry %s", path.name, exc_info=True)
            return None
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise TypeError("cache entry is not an object")
            if payload.get("schema") != CACHE_SCHEMA or payload.get("fingerprint") != self.fingerprint:
                self._discard(path)
                return None
            stored_at = float(payload["stored_at"])
            if not math.isfinite(stored_at) or self._expired(stored_at):
                self._discard(path)
                return None
            invoice, sources, ocr = payload["invoice"], payload["sources"], payload["ocr"]
            if not isinstance(invoice, dict) or not isinstance(sources, dict) or not isinstance(ocr, dict):
                raise TypeError("cache entry fields have the wrong type")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            self._discard(path)
            return None
        return CachedParse(invoice=invoice, sources=sources, ocr=ocr, stored_at=stored_at)

    def _discard(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove parse cache entry %s", path.name, exc_info=True)
