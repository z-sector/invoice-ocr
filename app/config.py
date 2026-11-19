"""Service settings.

Recognition and upload limits below are plain constants: edit this file to change them.
The Docker image contains this file and the models it selects, so a constant takes effect after a rebuild (`make up`).

The parse cache is the exception. Its lifetime and directory come from the environment
(`PARSE_CACHE_TTL_HOURS`, in hours, default 24; `PARSE_CACHE_DIR`). A restart is enough
for those two; the constants still need a rebuild.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

# --- Model -------------------------------------------------------------------

# The service recognizes English only. PaddleOCR picks the detection and recognition models for this language.
OCR_LANG = "en"
OCR_DEVICE = "cpu"
USE_DOC_ORIENTATION_CLASSIFY = False
USE_DOC_UNWARPING = False
# Lets rotated lines (e.g. vertical stamps) be read; costs one small extra model.
USE_TEXTLINE_ORIENTATION = True
# Load the model when the server starts, so the first upload does not wait for it.
PRELOAD_MODEL = True

# --- Input -------------------------------------------------------------------

# Larger uploads are rejected with 413.
MAX_UPLOAD_MB = 15
# Larger photos and PDF pages are scaled down to this length on the longest side.
MAX_IMAGE_SIDE = 2200
PDF_RENDER_DPI = 170
# Only the first pages of a long PDF are recognized.
MAX_PDF_PAGES = 10

# --- Parse cache -------------------------------------------------------------

# Lifetime of one cached parse when PARSE_CACHE_TTL_HOURS is unset or empty.
# The unit is hours. 24 hours is one day. Zero disables the cache.
DEFAULT_PARSE_CACHE_TTL_HOURS = 24.0


def parse_cache_ttl_hours(raw: str | None = None) -> float:
    """Return the cache lifetime in hours.

    An empty value means one day (24 hours). Zero disables the cache.
    A comma is accepted as the decimal separator (`1,5` is 1.5 hours).
    """
    if raw is None:
        raw = os.environ.get("PARSE_CACHE_TTL_HOURS")
    if raw is None or raw.strip() == "":
        return DEFAULT_PARSE_CACHE_TTL_HOURS
    text = raw.strip().replace(",", ".")
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(_ttl_hours_error(raw)) from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(_ttl_hours_error(raw))
    return value


def display_ttl_hours(value: float) -> int | float:
    """Whole numbers of hours stay integers so a response shows 24 and not 24.0."""
    number = float(value)
    if number.is_integer():
        return int(number)
    return number


def parse_cache_directory(raw: str | None = None) -> Path:
    """Directory of the on-disk parse cache. Unset means ~/.cache/invoice-ocr."""
    if raw is None:
        raw = os.environ.get("PARSE_CACHE_DIR")
    if raw is None or raw.strip() == "":
        return Path.home() / ".cache" / "invoice-ocr"
    return Path(raw.strip()).expanduser()


def _ttl_hours_error(raw: object) -> str:
    default = display_ttl_hours(DEFAULT_PARSE_CACHE_TTL_HOURS)
    return (
        "PARSE_CACHE_TTL_HOURS must be a number of hours "
        f"(empty uses the default of {default}; 0 disables the cache); got {raw!r}."
    )
