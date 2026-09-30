# syntax=docker/dockerfile:1.7
# One image, three roles: the API (default CMD), the arq worker and the migration job
# (override the command). Tesseract + the Korean model are needed for scanned 예산서 pages,
# and Nanum fonts for the synthetic demo documents.

ARG PYTHON_IMAGE=python:3.11-slim-bookworm
FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-kor fonts-nanum ffmpeg \
 && rm -rf /var/lib/apt/lists/*

FROM base AS build
RUN pip install --no-cache-dir uv==0.8.17
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first so source edits don't bust the layer cache.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra gcp --extra stt --no-install-project
COPY README.md alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra gcp --extra stt

FROM base AS runtime
RUN useradd --system --uid 10001 --home-dir /app app \
 && mkdir -p /app/.data \
 && chown app /app/.data
WORKDIR /app
COPY --from=build --chown=root:root /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/app/.data/huggingface \
    APP_STORAGE_URL=file:///app/.data/raw
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz', timeout=2)"
# Cloud Run injects PORT; forwarded headers come from its front end.
CMD ["sh", "-c", "exec uvicorn app.api.app:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*' --no-server-header"]
