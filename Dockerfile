FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv

# libgomp1: PaddlePaddle. libgl1 + libglib2.0-0: OpenCV, which PaddleOCR imports.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 app \
    && mkdir /app \
    && chown app:app /app
USER app
WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

COPY --chown=app:app pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Download the models into the image (~/.paddlex), so the container starts without network access.
# Only the files that select the models are copied first: other code changes do not repeat the download.
COPY --chown=app:app app/__init__.py app/config.py app/ocr.py ./app/
RUN python -c "from app.ocr import OcrEngine; OcrEngine().load()"

COPY --chown=app:app app ./app

EXPOSE 8741
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8741"]
