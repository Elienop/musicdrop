# ── Frontend build ────────────────────────────────────────────────────────
FROM node:22-alpine AS frontend-builder
ARG APP_VERSION=dev
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ── Backend deps (uv -> venv) ─────────────────────────────────────────────
FROM python:3.12-slim AS backend-builder
COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /usr/local/bin/
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
# Base deps only — the dev extra (mypy/ruff/pytest) stays out of the image.
RUN uv sync --frozen --no-cache

# ── Runtime ───────────────────────────────────────────────────────────────
FROM python:3.12-slim
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
    MUSICDROP_COVER_THUMB_CACHE_DIR=/data/cache/cover-thumbs \
    MUSICDROP_VERSION=${APP_VERSION}

VOLUME /data
EXPOSE 3030

# Cheap liveness via the API. start-period covers first-boot beets setup on
# a slow NAS volume; the check is observational (no restart-on-unhealthy).
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3030/api/health', timeout=4).status==200 else 1)"

# No USER directive, so the image's declared default user stays root — SonarQube
# docker:S6471, accepted (the one finding accepted in the 2026-08 compliance
# pass). The SERVING process is not root: entrypoint.sh remaps musicdrop to the
# operator's PUID/PGID, chowns /data, then drops via `exec gosu musicdrop`.
# A compliant image was built and verified, so this is a cost decision and not
# an impossibility: bake the user at build time (useradd at 911, then USER
# musicdrop) and remap only when started as root. A BARE `USER musicdrop` would
# not work — the user is created at runtime by entrypoint.sh, so the container
# would fail to start. It was declined because it breaks pull-and-restart
# upgrades for any operator whose PUID differs from that baked 911 (unRAID's
# convention is 99): /data would then refuse the new uid. See BACKLOG.md.
ENTRYPOINT ["/entrypoint.sh"]
# One worker by design: beets runs in-process and serial; extra workers
# would each open the library and duplicate the embedded beets state.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3030"]
