"""FastAPI service: accept an invoice image or PDF, run PaddleOCR on CPU, return parsed fields as JSON."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .cache import ParseCache, content_key
from .config import (
    MAX_UPLOAD_MB,
    OCR_DEVICE,
    OCR_LANG,
    PRELOAD_MODEL,
    display_ttl_hours,
    parse_cache_directory,
    parse_cache_ttl_hours,
)
from .ocr import PDF_TYPES, DocumentDecodeError, OcrEngine, pdf_magic
from .parser import parse_invoice

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("invoice_ocr")

MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/bmp", "image/tiff", "image/gif"}
NEUTRAL_TYPES = {"", "application/octet-stream", "binary/octet-stream"}

engine = OcrEngine()
cache = ParseCache(parse_cache_directory(), parse_cache_ttl_hours())


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info(
        "Parse cache directory %s, lifetime %s h",
        cache.directory,
        display_ttl_hours(cache.ttl_hours),
    )
    if cache.enabled:
        await run_in_threadpool(cache.purge_expired)
    if PRELOAD_MODEL:
        await run_in_threadpool(engine.load)
    yield


app = FastAPI(
    title="Invoice OCR",
    description="Upload an invoice image or PDF and extract payment fields with PaddleOCR on CPU.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "device": OCR_DEVICE,
        "lang": OCR_LANG,
        "model_loaded": engine.is_ready,
        "cache_ttl_hours": display_ttl_hours(cache.ttl_hours),
    }


@app.post("/api/parse")
async def parse(file: UploadFile = File(...)) -> JSONResponse:
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    filename = file.filename or ""

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File is too large. The limit is {MAX_UPLOAD_MB} MB.")

    as_pdf = _is_pdf_upload(data, content_type, filename)
    if not as_pdf and content_type not in IMAGE_TYPES and content_type not in NEUTRAL_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type: {content_type}. Upload a JPG, PNG, WEBP, BMP, TIFF image or a PDF.",
        )

    file_meta = {
        "name": file.filename,
        "size": len(data),
        "content_type": content_type,
        "kind": "pdf" if as_pdf else "image",
    }
    started = time.perf_counter()
    key = content_key(data)
    cached = await _cache_get(key)
    if cached is not None:
        total_ms = int((time.perf_counter() - started) * 1000)
        age = cache.age_seconds(cached.stored_at)
        logger.info("Parse cache hit for %s, age %ss", file.filename, age)
        return JSONResponse(
            _response_body(
                invoice=cached.invoice,
                sources=cached.sources,
                ocr=cached.ocr,
                total_ms=total_ms,
                file_meta=file_meta,
                cache_meta=_cache_meta(hit=True, age_seconds=age),
            )
        )

    try:
        ocr_result = await run_in_threadpool(engine.recognize, data, pdf=as_pdf)
    except DocumentDecodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        logger.exception("OCR failed for %s", file.filename)
        raise HTTPException(status_code=500, detail="Text recognition failed. Try another file.")

    parsed = parse_invoice(ocr_result)
    mean_score = (
        sum(s.score for s in ocr_result.segments) / len(ocr_result.segments) if ocr_result.segments else 0.0
    )
    invoice = parsed.invoice.to_dict()
    ocr_body = {
        "lines": [line.text for line in parsed.lines],
        "line_count": len(parsed.lines),
        "mean_confidence": round(mean_score, 3),
        "image_size": [ocr_result.image_width, ocr_result.image_height],
        "page_count": ocr_result.page_count,
        "pages_in_file": ocr_result.pages_in_file,
        "ocr_ms": ocr_result.duration_ms,
        "engine": f"PaddleOCR ({OCR_DEVICE.upper()}, lang={OCR_LANG})",
    }
    if await _cache_put(key, invoice=invoice, sources=parsed.sources, ocr=ocr_body):
        logger.info("Cached parse of %s", file.filename)
    total_ms = int((time.perf_counter() - started) * 1000)
    return JSONResponse(
        _response_body(
            invoice=invoice,
            sources=parsed.sources,
            ocr=ocr_body,
            total_ms=total_ms,
            file_meta=file_meta,
            cache_meta=_cache_meta(hit=False),
        )
    )


async def _cache_get(key: str):
    try:
        return await run_in_threadpool(cache.get, key)
    except Exception:
        logger.exception("Parse cache lookup failed")
        return None


async def _cache_put(key: str, **payload: dict) -> bool:
    try:
        return await run_in_threadpool(cache.put, key, **payload)
    except Exception:
        logger.exception("Parse cache store failed")
        return False


def _cache_meta(*, hit: bool, age_seconds: int | None = None) -> dict:
    meta: dict = {"hit": hit, "ttl_hours": display_ttl_hours(cache.ttl_hours)}
    if age_seconds is not None:
        meta["age_seconds"] = age_seconds
    return meta


def _response_body(
    *,
    invoice: dict,
    sources: dict,
    ocr: dict,
    total_ms: int,
    file_meta: dict,
    cache_meta: dict,
) -> dict:
    return {
        "invoice": invoice,
        "sources": sources,
        "ocr": {**ocr, "total_ms": total_ms},
        "file": file_meta,
        "cache": cache_meta,
    }


def _is_pdf_upload(data: bytes, content_type: str, filename: str) -> bool:
    if pdf_magic(data):
        return True
    named_pdf = filename.lower().endswith(".pdf")
    if content_type in PDF_TYPES:
        return True
    # Clients often send octet-stream; trust a .pdf name in that case.
    return named_pdf and content_type in NEUTRAL_TYPES
