# syntax=docker/dockerfile:1.7
# The application image plus the Render runtime adapter. Context is the repository root.
ARG PYTHON_IMAGE=python:3.11-slim-bookworm
FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-kor fonts-nanum \
 && rm -rf /var/lib/apt/lists/*

FROM base AS build
RUN pip install --no-cache-dir uv==0.8.17
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY apps/api/pyproject.toml apps/api/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra gcp --no-install-project
COPY apps/api/README.md apps/api/alembic.ini ./
COPY apps/api/migrations ./migrations
COPY apps/api/src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --extra gcp

FROM base AS runtime
RUN useradd --system --uid 10001 --home-dir /app app
WORKDIR /app
COPY --from=build --chown=root:root /app /app
COPY scripts/render-runtime.py /app/render-runtime.py
ENV PATH="/app/.venv/bin:$PATH"
USER app
EXPOSE 10000
CMD ["python", "/app/render-runtime.py", "api"]
