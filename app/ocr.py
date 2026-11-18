"""PaddleOCR wrapper: CPU-only text recognition that returns positioned text lines.

Images are decoded with Pillow. PDFs are rasterized with pypdfium2 (one image per page)
and recognized page by page; coordinates are stacked so the parser still sees one document.
"""

from __future__ import annotations

import io
import logging
import os
import threading
import time
from dataclasses import dataclass, replace

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

# Avoid a slow network probe on every start; models are cached after the first download.
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

logger = logging.getLogger(__name__)

MAX_IMAGE_SIDE = int(os.environ.get("OCR_MAX_IMAGE_SIDE", "2200"))
PDF_RENDER_DPI = int(os.environ.get("OCR_PDF_DPI", "170"))
MAX_PDF_PAGES = int(os.environ.get("OCR_MAX_PDF_PAGES", "10"))
# Keeps the last line of one page from merging with the first line of the next.
PAGE_GAP_PX = 48
PDF_TYPES = {"application/pdf", "application/x-pdf"}
OCR_LANG = os.environ.get("OCR_LANG", "en")
# Languages the UI offers. Each one loads its own recognition model on first use.
OCR_LANGS = tuple(code.strip() for code in os.environ.get("OCR_LANGS", "en,ru").split(",") if code.strip())
LANG_LABELS = {
    "en": "English / Latin",
    "ru": "Русский + латиница",
    "ch": "中文 + English",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
    "it": "Italiano",
    "uk": "Українська",
    "japan": "日本語",
    "korean": "한국어",
}


class DocumentDecodeError(ValueError):
    """Raised when the upload is not a readable image or PDF."""


class ImageDecodeError(DocumentDecodeError):
    """Raised when the uploaded bytes are not a readable raster image."""


@dataclass(frozen=True)
class TextSegment:
    """One text box detected by PaddleOCR, in image pixel coordinates."""

    text: str
    score: float
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass(frozen=True)
class OcrResult:
    segments: list[TextSegment]
    image_width: int
    image_height: int
    duration_ms: int
    page_count: int = 1
    pages_in_file: int = 1


class OcrEngine:
    """Lazily loads PaddleOCR on CPU and serialises inference calls (the predictor is not thread-safe)."""

    def __init__(self, lang: str = OCR_LANG) -> None:
        self._lang = lang
        self._ocr = None
        self._lock = threading.Lock()

    @property
    def lang(self) -> str:
        return self._lang

    @property
    def is_ready(self) -> bool:
        return self._ocr is not None

    def load(self) -> None:
        if self._ocr is not None:
            return
        with self._lock:
            if self._ocr is not None:
                return
            from paddleocr import PaddleOCR

            started = time.perf_counter()
            logger.info("Loading PaddleOCR (lang=%s, device=cpu)...", self._lang)
            self._ocr = PaddleOCR(
                lang=self._lang,
                device="cpu",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=True,
            )
            logger.info("PaddleOCR ready in %.1fs", time.perf_counter() - started)

    def recognize(self, data: bytes, *, pdf: bool = False) -> OcrResult:
        if pdf or pdf_magic(data):
            images, pages_in_file = render_pdf(data)
        else:
            images, pages_in_file = [_decode_image(data)], 1
        return self._recognize_images(images, pages_in_file)

    def _recognize_images(self, images: list[Image.Image], pages_in_file: int) -> OcrResult:
        self.load()
        started = time.perf_counter()
        segments: list[TextSegment] = []
        y_offset = 0.0
        max_width = 0
        # The predictor is not thread-safe; hold the lock for the whole document.
        with self._lock:
            for index, image in enumerate(images):
                for segment in _segments_from_outputs(self._ocr.predict(np.asarray(image))):
                    if y_offset:
                        segment = replace(segment, y0=segment.y0 + y_offset, y1=segment.y1 + y_offset)
                    segments.append(segment)
                max_width = max(max_width, image.width)
                y_offset += image.height
                if index != len(images) - 1:
                    y_offset += PAGE_GAP_PX
        return OcrResult(
            segments=segments,
            image_width=max_width,
            image_height=int(y_offset),
            duration_ms=int((time.perf_counter() - started) * 1000),
            page_count=len(images),
            pages_in_file=pages_in_file,
        )


class EngineRegistry:
    """One OcrEngine per language, created on demand."""

    def __init__(self, langs: tuple[str, ...] = OCR_LANGS, default: str = OCR_LANG) -> None:
        self.langs = tuple(dict.fromkeys((default, *langs)))
        self.default = default
        self._engines: dict[str, OcrEngine] = {}
        self._lock = threading.Lock()

    def get(self, lang: str | None) -> OcrEngine:
        lang = (lang or self.default).strip().lower()
        if lang not in self.langs:
            raise KeyError(lang)
        with self._lock:
            engine = self._engines.get(lang)
            if engine is None:
                engine = self._engines[lang] = OcrEngine(lang)
        return engine

    def loaded(self) -> list[str]:
        return [lang for lang, engine in self._engines.items() if engine.is_ready]


def pdf_magic(data: bytes) -> bool:
    """True when the payload looks like a PDF, allowing a short preamble before the header."""
    return b"%PDF-" in data[:1024]


def render_pdf(data: bytes) -> tuple[list[Image.Image], int]:
    """Rasterize up to MAX_PDF_PAGES pages. Returns (images, total page count in the file)."""
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise _pdf_open_error(exc) from exc

    try:
        try:
            pdf.init_forms()
        except Exception:
            logger.debug("PDF form initialization skipped", exc_info=True)

        total = len(pdf)
        if total == 0:
            raise DocumentDecodeError("The PDF has no pages.")

        images: list[Image.Image] = []
        for index in range(min(total, MAX_PDF_PAGES)):
            page = pdf[index]
            try:
                image = _render_page(page)
            except pdfium.PdfiumError as exc:
                raise DocumentDecodeError(f"Could not render PDF page {index + 1}.") from exc
            finally:
                page.close()
            if image is not None:
                images.append(image)
        if not images:
            raise DocumentDecodeError("The PDF pages could not be rendered.")
        if total > MAX_PDF_PAGES:
            logger.info("PDF has %s pages; recognizing the first %s", total, MAX_PDF_PAGES)
        return images, total
    finally:
        pdf.close()


def _pdf_open_error(exc: Exception) -> DocumentDecodeError:
    message = str(exc).lower()
    if "password" in message:
        return DocumentDecodeError("This PDF is password-protected. Upload a file without a password.")
    return DocumentDecodeError("The PDF could not be opened. The file may be damaged.")


def _render_page(page) -> Image.Image | None:
    width_pt, height_pt = page.get_size()
    longest_pt = max(width_pt, height_pt)
    if longest_pt <= 1:
        return None
    scale = min(PDF_RENDER_DPI / 72.0, MAX_IMAGE_SIDE / longest_pt)
    bitmap = page.render(scale=scale, fill_color=(255, 255, 255, 255))
    try:
        image = bitmap.to_pil()
    finally:
        bitmap.close()
    return _fit_image(_to_rgb(image))


def _segments_from_outputs(outputs) -> list[TextSegment]:
    segments: list[TextSegment] = []
    for out in outputs:
        texts = out.get("rec_texts") or []
        scores = out.get("rec_scores") or []
        polys = out.get("rec_polys")
        if polys is None or len(polys) == 0:
            polys = out.get("dt_polys") or []
        for text, score, poly in zip(texts, scores, polys):
            text = str(text).strip()
            if not text:
                continue
            pts = np.asarray(poly, dtype=float).reshape(-1, 2)
            segments.append(
                TextSegment(
                    text=text,
                    score=float(score),
                    x0=float(pts[:, 0].min()),
                    y0=float(pts[:, 1].min()),
                    x1=float(pts[:, 0].max()),
                    y1=float(pts[:, 1].max()),
                )
            )
    return segments


def _decode_image(data: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageDecodeError("The file is not a supported image (use JPG, PNG, WEBP, BMP or TIFF).") from exc

    # Phone cameras store rotation in EXIF; apply it so the text is upright.
    image = ImageOps.exif_transpose(image)
    return _fit_image(_to_rgb(image))


def _to_rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGBA":
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        return background
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def _fit_image(image: Image.Image) -> Image.Image:
    longest = max(image.size)
    if longest <= MAX_IMAGE_SIDE:
        return image
    scale = MAX_IMAGE_SIDE / longest
    new_size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(new_size, Image.Resampling.LANCZOS)
