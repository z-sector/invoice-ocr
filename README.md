# Invoice OCR API (FastAPI + PaddleOCR, CPU)

An HTTP API that extracts payment fields from an invoice. Upload a photo or a PDF, PaddleOCR (PaddlePaddle, CPU only)
recognizes the text, and a rule-based parser returns the fields as JSON:

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

On the first start the container downloads the OCR models (about 140 MB for English) from Hugging Face into the
`invoice-ocr-models` Docker volume; later starts reuse them and take a few seconds.

## Makefile

Run `make` (or `make help`) to list the targets.

| Target | What it does |
| --- | --- |
| `make build` | Build the image `invoice-ocr:latest` |
| `make rebuild` | Build without the layer cache, pulling fresh base images |
| `make run` | (Re)start the container in the background from the built image |
| `make up` | `build`, then `run` |
| `make stop` | Stop and remove the container |
| `make restart` | `stop`, then `run` |
| `make logs` | Follow the container logs |
| `make status` | Show the container state and health |
| `make shell` | Open a shell inside the running container |
| `make clean` | Remove the container and the image |
| `make clean-models` | Remove the container and the volume with downloaded models |
| `make health` | `GET /health` |
| `make wait` | Block until `/health` answers (fails after `WAIT_TIMEOUT` seconds) |
| `make parse FILE=... [DOC_LANG=ru]` | `POST /api/parse` with the given file |
| `make install` | Local development: create `.venv` from `uv.lock` |
| `make dev` | Local development: run the API with auto-reload |
| `make lock` | Local development: re-resolve `uv.lock` after editing `pyproject.toml` |

Variables, passed as `make <target> NAME=value`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `8741` | Host port (`run`, `up`, `dev`) and the port the API targets call |
| `IMAGE`, `TAG` | `invoice-ocr`, `latest` | Image name and tag |
| `CONTAINER` | `invoice-ocr` | Container name |
| `MODELS_VOLUME` | `invoice-ocr-models` | Docker volume that keeps downloaded models |
| `MODEL_LANGS` | empty | Languages whose models are baked into the image at build time, e.g. `en,ru` |
| `ENV_FILE` | `.env` | File with service settings, passed to the container when it exists |
| `URL` | `http://localhost:$(PORT)` | Base URL used by `health`, `wait` and `parse` |
| `WAIT_TIMEOUT` | `300` | Seconds `make wait` keeps trying |
| `DOCKER` | `docker` | Container CLI |

`make run` does not rebuild: after changing the code run `make up`.

## API

Interactive OpenAPI documentation is served at `/docs` (Swagger UI) and `/redoc`; the schema is at `/openapi.json`.

### `POST /api/parse`

Recognizes a document and returns the extracted fields. The request is `multipart/form-data`:

| Field | Required | Description |
| --- | --- | --- |
| `file` | yes | The invoice: an image (JPG, PNG, WEBP, BMP, TIFF, GIF) or a PDF, up to `MAX_UPLOAD_MB` (15 MB by default) |
| `lang` | no | Document language, one of `OCR_LANGS`. Defaults to `OCR_LANG` (`en`). The `ru` model also reads Latin text |

```bash
curl -s -F "file=@invoice.jpg" -F "lang=en" http://localhost:8741/api/parse
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
| `ocr.page_count`, `ocr.pages_in_file` | Pages that were recognized and pages in the file (a PDF is cut at `OCR_MAX_PDF_PAGES`) |
| `ocr.ocr_ms`, `ocr.total_ms` | Recognition time and total processing time, in milliseconds |
| `file.kind` | `image` or `pdf` |

Errors are returned as `{"detail": "..."}`:

| Status | Reason |
| --- | --- |
| `400` | The file is empty, is not a readable image, or the PDF is damaged, password-protected or has no pages |
| `413` | The file is larger than `MAX_UPLOAD_MB` |
| `415` | The content type is neither an image nor a PDF |
| `422` | `lang` is not one of `OCR_LANGS`, or the `file` field is missing |
| `500` | Text recognition failed |

Recognition takes 2–20 seconds per page on a CPU. Requests for the same language are processed one at a time.

### `GET /health`

```json
{ "status": "ok", "device": "cpu", "languages": ["en", "ru"], "default_lang": "en", "loaded": ["en"] }
```

`languages` are the accepted values of `lang`; `loaded` lists the languages whose models are already in memory.
With `OCR_PRELOAD=1` the server starts listening only after the default model is loaded, so a response also means the
service is ready.

## Configuration

The service is configured with environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `OCR_LANG` | `en` | Default language; its model is loaded at startup |
| `OCR_LANGS` | `en,ru` | Languages accepted in `lang`; each model is loaded on first use |
| `OCR_PRELOAD` | `1` | `0` loads the default model on the first request instead of at startup |
| `OCR_MAX_IMAGE_SIDE` | `2200` | Larger images and PDF pages are downscaled to this size on the longest side |
| `OCR_PDF_DPI` | `170` | Resolution used to rasterize PDF pages |
| `OCR_MAX_PDF_PAGES` | `10` | Number of leading PDF pages to recognize; the rest are skipped |
| `MAX_UPLOAD_MB` | `15` | Upload size limit |

For Docker, copy `.env.example` to `.env` and edit it; `make run` passes the file to the container. For a local run,
export the variables before `make dev`.

## Docker image

The `Dockerfile` is a two-stage build on `python:3.12-slim-bookworm`: the first stage installs the locked dependencies
with uv into a virtualenv, the second one copies the virtualenv and `app/` and runs uvicorn on port 8741 as the
non-root user `app`. The image is about 1.7 GB unpacked (520 MB compressed), most of it PaddlePaddle. It has a
`HEALTHCHECK` that calls `/health`.

Models are stored in `/home/app/.paddlex`. There are two ways to provide them:

- A volume (the default of `make run`): models are downloaded on first use and kept in the `invoice-ocr-models` volume.
  The container needs internet access the first time a language is used.
- Baked into the image: `make up MODEL_LANGS=en,ru` downloads the models at build time. The container then starts
  without network access and without a volume. Each language adds roughly 90–140 MB.

The same without Make:

```bash
docker build -t invoice-ocr .
docker run -d --name invoice-ocr -p 8741:8741 -v invoice-ocr-models:/home/app/.paddlex invoice-ocr
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
app/ocr.py       PaddleOCR wrapper, image decoding, PDF rasterization
app/parser.py    Rule-based field extraction
Dockerfile       Container image
Makefile         Build, run and maintenance commands
.env.example     Service settings template
pyproject.toml   Dependencies (locked in uv.lock)
```

## How parsing works

`app/ocr.py` runs PaddleOCR (`device="cpu"`: text detection, text line orientation classification, recognition) and
returns text boxes with coordinates. A PDF is first rasterized with `pypdfium2`, page by page up to
`OCR_MAX_PDF_PAGES`; the coordinates of later pages are shifted down, so the document is parsed as one page.
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
