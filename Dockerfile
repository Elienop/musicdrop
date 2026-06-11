# ── Frontend build ────────────────────────────────────────────────────────
FROM node:22-alpine AS frontend-builder
ARG APP_VERSION=dev
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ── Backend deps (uv -> venv) ─────────────────────────────────────────────
FROM python:3.11-slim AS backend-builder
COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /usr/local/bin/
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
# Base deps only — the dev extra (mypy/ruff/pytest) stays out of the image.
RUN uv sync --frozen --no-cache

# ── Runtime ───────────────────────────────────────────────────────────────
FROM python:3.11-slim
ARG APP_VERSION=dev
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=backend-builder /app/.venv /app/.venv
COPY backend/app /app/app
COPY --from=frontend-builder /build/dist /app/static
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    MUSICDROP_STATIC_DIR=/app/static \
    MUSICDROP_BEETS_DIR=/data/beets \
    MUSICDROP_ARTIST_IMAGE_CACHE_DIR=/data/cache/artist-images \
    MUSICDROP_VERSION=${APP_VERSION}

VOLUME /data
EXPOSE 3030

# Cheap liveness via the API. start-period covers first-boot beets setup on
# a slow NAS volume; the check is observational (no restart-on-unhealthy).
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3030/api/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["/entrypoint.sh"]
# One worker by design: beets runs in-process and serial; extra workers
# would each open the library and duplicate the embedded beets state.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3030"]
