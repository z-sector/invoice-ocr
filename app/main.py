"""FastAPI service: accept an invoice image or PDF, run PaddleOCR on CPU, return parsed fields as JSON."""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .ocr import PDF_TYPES, DocumentDecodeError, EngineRegistry, pdf_magic
from .parser import parse_invoice

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("invoice_ocr")

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "15")) * 1024 * 1024
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/bmp", "image/tiff", "image/gif"}
NEUTRAL_TYPES = {"", "application/octet-stream", "binary/octet-stream"}
PRELOAD_MODELS = os.environ.get("OCR_PRELOAD", "1") != "0"

registry = EngineRegistry()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if PRELOAD_MODELS:
        # Warm up the default language at startup so the first upload does not wait for model loading.
        await run_in_threadpool(registry.get(registry.default).load)
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
        "device": "cpu",
        "languages": list(registry.langs),
        "default_lang": registry.default,
        "loaded": registry.loaded(),
    }


@app.post("/api/parse")
async def parse(file: UploadFile = File(...), lang: str | None = Form(default=None)) -> JSONResponse:
    try:
        engine = registry.get(lang)
    except KeyError:
        raise HTTPException(status_code=422, detail=f"Unsupported language '{lang}'. Available: {', '.join(registry.langs)}.")

    content_type = (file.content_type or "").split(";")[0].strip().lower()
    filename = file.filename or ""

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File is too large. The limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")

    as_pdf = _is_pdf_upload(data, content_type, filename)
    if not as_pdf and content_type not in IMAGE_TYPES and content_type not in NEUTRAL_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type: {content_type}. Upload a JPG, PNG, WEBP, BMP, TIFF image or a PDF.",
        )

    started = time.perf_counter()
    try:
        ocr_result = await run_in_threadpool(engine.recognize, data, pdf=as_pdf)
    except DocumentDecodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        logger.exception("OCR failed for %s", file.filename)
        raise HTTPException(status_code=500, detail="Text recognition failed. Try another file.")

    parsed = parse_invoice(ocr_result)
    total_ms = int((time.perf_counter() - started) * 1000)
    mean_score = (
        sum(s.score for s in ocr_result.segments) / len(ocr_result.segments) if ocr_result.segments else 0.0
    )

    return JSONResponse(
        {
            "invoice": parsed.invoice.to_dict(),
            "sources": parsed.sources,
            "ocr": {
                "lines": [line.text for line in parsed.lines],
                "line_count": len(parsed.lines),
                "mean_confidence": round(mean_score, 3),
                "image_size": [ocr_result.image_width, ocr_result.image_height],
                "page_count": ocr_result.page_count,
                "pages_in_file": ocr_result.pages_in_file,
                "ocr_ms": ocr_result.duration_ms,
                "total_ms": total_ms,
                "engine": f"PaddleOCR (CPU, lang={engine.lang})",
            },
            "file": {
                "name": file.filename,
                "size": len(data),
                "content_type": content_type,
                "kind": "pdf" if as_pdf else "image",
            },
        }
    )


def _is_pdf_upload(data: bytes, content_type: str, filename: str) -> bool:
    if pdf_magic(data):
        return True
    named_pdf = filename.lower().endswith(".pdf")
    if content_type in PDF_TYPES:
        return True
    # Clients often send octet-stream; trust a .pdf name in that case.
    return named_pdf and content_type in NEUTRAL_TYPES
