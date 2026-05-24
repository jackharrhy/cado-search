# syntax=docker/dockerfile:1.10
#
# Multi-stage build for cado.
#
# - The "builder" stage uses the official uv image to resolve and install the
#   locked dependencies into a virtualenv at /app/.venv.
# - The runtime stage is a slim python image that copies just the venv and
#   the package source. It runs as a non-root user and exposes the HTMX UI
#   on port 8000.
#
# Data lives under /data (mount a volume there); CADO_DATA_DIR points to it.

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.8.15

# ---------------------------------------------------------------------------
# builder
# ---------------------------------------------------------------------------

FROM ghcr.io/astral-sh/uv:${UV_VERSION}-python${PYTHON_VERSION}-bookworm-slim AS builder

# uv tunables (https://docs.astral.sh/uv/reference/settings/):
#   compile bytecode for faster cold starts
#   copy rather than symlink so we can move the venv between stages
#   never download a different Python at install time
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install dependencies first (without the project source) so the layer
# caches independently of code edits.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=README.md,target=README.md \
    uv sync --frozen --no-install-project --no-dev

# Then install the project itself.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

# Minimal runtime libs. lxml needs libxml2 / libxslt at runtime, but the
# bundled wheels for our target platforms (linux/amd64, linux/arm64) ship
# their own native code, so nothing else is required here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

# Create an unprivileged user that owns the venv + data dir.
RUN groupadd --system --gid 1000 cado \
    && useradd --system --uid 1000 --gid cado --create-home --shell /usr/sbin/nologin cado

WORKDIR /app

# Copy the prebuilt venv (Python is at the same major.minor in both stages)
# plus the project source it references via a .pth file -- uv installs the
# project as an editable by default, so /app/src must exist at runtime.
COPY --from=builder --chown=cado:cado /app/.venv /app/.venv
COPY --from=builder --chown=cado:cado /app/src /app/src

# Put venv binaries first so `cado` resolves to our entry point.
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CADO_DATA_DIR=/data

# Persistent data volume: HTML cache + DuckDB live here.
RUN mkdir -p /data && chown -R cado:cado /data
VOLUME ["/data"]

USER cado

EXPOSE 8000

# Lightweight HTTP healthcheck: the UI returns 200 if the DB is open.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request, sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/', timeout=3).status == 200 else 1)" \
    || exit 1

# tini handles PID 1 / signal forwarding so Ctrl-C on `docker run` exits cleanly.
ENTRYPOINT ["/usr/bin/tini", "--", "cado"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
