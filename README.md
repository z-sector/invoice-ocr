# Invoice OCR API (FastAPI + PaddleOCR, CPU)

An HTTP API that extracts payment fields from an invoice. Upload a photo or a PDF, PaddleOCR (PaddlePaddle, CPU only)
recognizes the text with its English model, and a rule-based parser returns the fields as JSON:

```json
{
  "vendor_name": "Blue Ridge Supply Co.",
  "invoice_number": "2026-1142",
  "amount": "2290.50",
  "currency": "USD",
  "bank_name_on_invoice": "Wells Fargo Bank, N.A.",
  "account_number_on_invoice": "4000123456789",
  "routing_on_invoice": "121000248"
}
```

The service is API only: there is no web UI. The fields correspond to the `ParsedInvoiceDS` dataclass in `app/parser.py`.

## Quick start (Docker)

Requirements: Docker, GNU Make, curl.

```bash
make up                              # build the image and start the container on port 8741
make wait                            # block until the service is ready
make parse FILE=path/to/invoice.pdf  # send a document, print the JSON response
```

The OCR models (about 140 MB) are downloaded from Hugging Face while the image is built and stored in it, so the
container starts in a few seconds and needs no network access.

## Makefile

Run `make` (or `make help`) to list the targets.

| Target | What it does |
| --- | --- |
| `make build` | Build the image `invoice-ocr:latest`, downloading the OCR models into it |
| `make rebuild` | Build without the layer cache, pulling fresh base images |
| `make run` | (Re)start the container in the background from the built image |
| `make up` | `build`, then `run` |
| `make stop` | Stop and remove the container |
| `make restart` | `stop`, then `run` |
| `make logs` | Follow the container logs |
| `make status` | Show the container state and health |
| `make shell` | Open a shell inside the running container |
| `make clean` | Remove the container and the image |
| `make health` | `GET /health` |
| `make wait` | Block until `/health` answers (fails after `WAIT_TIMEOUT` seconds) |
| `make parse FILE=...` | `POST /api/parse` with the given file |
| `make install` | Local development: create `.venv` from `uv.lock` |
| `make dev` | Local development: run the API with auto-reload |
| `make lock` | Local development: re-resolve `uv.lock` after editing `pyproject.toml` |

Variables, passed as `make <target> NAME=value`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `8741` | Host port (`run`, `up`, `dev`) and the port the API targets call |
| `IMAGE`, `TAG` | `invoice-ocr`, `latest` | Image name and tag |
| `CONTAINER` | `invoice-ocr` | Container name |
| `ENV_FILE` | `.env` | File with environment variables, passed to the container when it exists |
| `URL` | `http://localhost:$(PORT)` | Base URL used by `health`, `wait` and `parse` |
| `WAIT_TIMEOUT` | `300` | Seconds `make wait` keeps trying |
| `DOCKER` | `docker` | Container CLI |

`make run` does not rebuild: after changing the code or `app/config.py` run `make up`.

## API

Interactive OpenAPI documentation is served at `/docs` (Swagger UI) and `/redoc`; the schema is at `/openapi.json`.

### `POST /api/parse`

Recognizes a document and returns the extracted fields. The request is `multipart/form-data`:

| Field | Required | Description |
| --- | --- | --- |
| `file` | yes | The invoice: an image (JPG, PNG, WEBP, BMP, TIFF, GIF) or a PDF, up to `MAX_UPLOAD_MB` (15 MB by default) |

The document is recognized with the English model; there is no language parameter.

```bash
curl -s -F "file=@invoice.jpg" http://localhost:8741/api/parse
curl -s -F "file=@invoice.pdf" http://localhost:8741/api/parse
```

Response `200 OK`:

```json
{
  "invoice": {
    "vendor_name": "Blue Ridge Supply Co.",
    "invoice_number": "2026-1142",
    "amount": "2290.50",
    "currency": "USD",
    "bank_name_on_invoice": "Wells Fargo Bank, N.A.",
    "account_number_on_invoice": "4000123456789",
    "routing_on_invoice": "121000248"
  },
  "sources": {
    "vendor_name": "Blue Ridge Supply Co. INVOICE",
    "invoice_number": "Invoice No: 2026-1142",
    "amount": "Amount Due: $2,290.50",
    "currency": "Amount Due: $2,290.50",
    "bank_name_on_invoice": "Bank: Wells Fargo Bank, N.A.",
    "account_number_on_invoice": "Account Number: 4000123456789",
    "routing_on_invoice": "Routing Number: 121000248"
  },
  "ocr": {
    "lines": ["Blue Ridge Supply Co. INVOICE", "Invoice No: 2026-1142", "..."],
    "line_count": 16,
    "mean_confidence": 0.998,
    "image_size": [1240, 1500],
    "page_count": 1,
    "pages_in_file": 1,
    "ocr_ms": 2615,
    "total_ms": 2633,
    "engine": "PaddleOCR (CPU, lang=en)"
  },
  "file": { "name": "invoice.png", "size": 82511, "content_type": "image/png", "kind": "image" }
}
```

| Key | Description |
| --- | --- |
| `invoice` | The extracted fields. A field that was not found is an empty string. `amount` is normalized to `1234.56`, `currency` is an ISO 4217 code |
| `sources` | For each field that was found, the OCR line it was taken from |
| `ocr.lines` | The recognized text, grouped into rows from top to bottom |
| `ocr.mean_confidence` | Average recognition confidence of the text boxes, from 0 to 1 |
| `ocr.image_size` | Width and height in pixels of the recognized image; PDF pages are stacked vertically |
| `ocr.page_count`, `ocr.pages_in_file` | Pages that were recognized and pages in the file (a PDF is cut at `MAX_PDF_PAGES`) |
| `ocr.ocr_ms`, `ocr.total_ms` | Recognition time and total processing time, in milliseconds |
| `file.kind` | `image` or `pdf` |

Errors are returned as `{"detail": "..."}`:

| Status | Reason |
| --- | --- |
| `400` | The file is empty, is not a readable image, or the PDF is damaged, password-protected or has no pages |
| `413` | The file is larger than `MAX_UPLOAD_MB` |
| `415` | The content type is neither an image nor a PDF |
| `422` | The `file` field is missing |
| `500` | Text recognition failed |

Recognition takes 2–20 seconds per page on a CPU. Requests are processed one at a time.

### `GET /health`

```json
{ "status": "ok", "device": "cpu", "lang": "en", "model_loaded": true }
```

`lang` is the language of the model; `model_loaded` tells whether it is already in memory. With `PRELOAD_MODEL = True`
the server starts listening only after the model is loaded, so a response also means the service is ready.

## Configuration

The OCR settings are constants in `app/config.py`. To change one, edit the file and rebuild the image (`make up`) or
restart the local server.

| Constant | Value | Purpose |
| --- | --- | --- |
| `OCR_LANG` | `"en"` | Language of the model. The service is built for English only |
| `OCR_DEVICE` | `"cpu"` | Device PaddleOCR runs on |
| `USE_DOC_ORIENTATION_CLASSIFY` | `False` | Detect the rotation of the whole document (a separate model) |
| `USE_DOC_UNWARPING` | `False` | Flatten curved pages (a separate model) |
| `USE_TEXTLINE_ORIENTATION` | `True` | Detect the orientation of individual text lines |
| `PRELOAD_MODEL` | `True` | Load the model at startup; `False` loads it on the first request |
| `MAX_IMAGE_SIDE` | `2200` | Larger images and PDF pages are downscaled to this size on the longest side |
| `PDF_RENDER_DPI` | `170` | Resolution used to rasterize PDF pages |
| `MAX_PDF_PAGES` | `10` | Number of leading PDF pages to recognize; the rest are skipped |

The upload size limit is the only environment variable: `MAX_UPLOAD_MB` (default `15`). For Docker, copy
`.env.example` to `.env` and edit it; `make run` passes the file to the container. For a local run, export the
variable before `make dev`.

## Docker image

The `Dockerfile` is a two-stage build on `python:3.12-slim-bookworm`: the first stage installs the locked dependencies
with uv into a virtualenv, the second one copies the virtualenv and `app/` and runs uvicorn on port 8741 as the
non-root user `app`. The image is about 1.8 GB unpacked (630 MB compressed), most of it PaddlePaddle; the
models take 140 MB. It has a `HEALTHCHECK` that calls `/health`.

The models are downloaded at build time into `/home/app/.paddlex` inside the image, so the build needs internet
access and the container does not: it starts without network access and without a volume.

The same without Make:

```bash
docker build -t invoice-ocr .
docker run -d --name invoice-ocr -p 8741:8741 invoice-ocr
```

## Local development

Dependencies are managed with [uv](https://docs.astral.sh/uv/) (`pyproject.toml` + `uv.lock`). Python 3.11–3.13 is
required; uv downloads a suitable interpreter if needed. All packages, including the CPU build of `paddlepaddle`, come
from PyPI.

```bash
make install    # uv sync --locked
make dev        # uvicorn with auto-reload on http://localhost:8741
```

Models are cached in `~/.paddlex/official_models/`. Add a dependency with `uv add <package>`; after editing
`pyproject.toml` by hand run `make lock`.

## Project layout

```
app/main.py      FastAPI application: endpoints, upload validation
app/config.py    OCR settings (constants)
app/ocr.py       PaddleOCR wrapper, image decoding, PDF rasterization
app/parser.py    Rule-based field extraction
Dockerfile       Container image
Makefile         Build, run and maintenance commands
.env.example     Environment variables template
pyproject.toml   Dependencies (locked in uv.lock)
```

## How parsing works

`app/ocr.py` runs PaddleOCR (`device="cpu"`: text detection, text line orientation classification, recognition) and
returns text boxes with coordinates. A PDF is first rasterized with `pypdfium2`, page by page up to
`MAX_PDF_PAGES`; the coordinates of later pages are shifted down, so the document is parsed as one page.
`app/parser.py` groups the boxes into rows by vertical position and then applies rules:

- `invoice_number`: labels such as `Invoice No / # / № / N°`, `Счет на оплату №`, `Facture N°`.
- `amount` and `currency`: rows with `Total due / Amount due / Grand total / Итого к оплате`, then any `Total`,
  excluding `Subtotal` and `Tax / VAT / НДС`. Without labels the largest money amount is used. The currency comes from
  a symbol or a code (`$`, `€`, `USD`, `руб.`).
- `bank_name_on_invoice`: labels `Bank / Bank name / Beneficiary bank / Банк получателя`, a list of well-known banks,
  phrases like `... Bank, N.A.`.
- `account_number_on_invoice`: `Account # / Acct / A/C / IBAN / Р/с`.
- `routing_on_invoice`: `Routing / ABA / БИК`, then `Sort code / SWIFT / BIC / IFSC / BSB`.
- `vendor_name`: labels `From / Remit to / Beneficiary / Поставщик`, otherwise the largest text in the upper part of
  the page, preferring legal forms (`LLC`, `Ltd.`, `GmbH`, `ООО`) and skipping the `Bill To` block.
