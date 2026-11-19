# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.13.9

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv


# ---- builder: resolve the locked dependencies into a virtualenv ----
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-dev --no-install-project


# ---- runtime ----
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

# Shared libraries PaddlePaddle (OpenMP) and OpenCV load at import time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PADDLE_PDX_CACHE_HOME=/home/app/.paddlex \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True

RUN useradd --create-home --uid 1000 app \
    && mkdir -p "$PADDLE_PDX_CACHE_HOME" \
    && chown app:app "$PADDLE_PDX_CACHE_HOME"

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

USER app

# Download the models into the image, so the container starts without network access or a volume.
# Only the modules that select the models are copied first: other code changes do not repeat the download.
COPY app/__init__.py app/config.py app/ocr.py ./app/
RUN python -c "from app.ocr import OcrEngine; OcrEngine().load()"

COPY app ./app

# Owned by `app` (the USER above) so a new named volume inherits that owner.
RUN mkdir -p /home/app/.cache/invoice-ocr

# Lifetime is hours. Empty or unset is the same 24-hour default inside the app.
ENV PARSE_CACHE_TTL_HOURS=24 \
    PARSE_CACHE_DIR=/home/app/.cache/invoice-ocr

EXPOSE 8741

# The server only starts listening once the model is loaded, so "healthy" also means "ready".
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8741/health', timeout=3)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8741"]
