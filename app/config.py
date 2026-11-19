"""OCR settings. Every value is a plain constant: edit this file to change how recognition runs.

The Docker image downloads the models these settings select at build time, so rebuild the image after
changing anything in the "Model" block.
"""

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

# Larger photos and PDF pages are scaled down to this length on the longest side.
MAX_IMAGE_SIDE = 2200
PDF_RENDER_DPI = 170
# Only the first pages of a long PDF are recognized.
MAX_PDF_PAGES = 10
