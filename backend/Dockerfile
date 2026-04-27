# syntax=docker/dockerfile:1.7

# ---- Stage 1: build wheels -------------------------------------------------
# Build wheels in a fat image so the runtime stage stays lean.
FROM --platform=linux/arm64 python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build toolchain only needed to compile any C extensions during wheel build.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy dependency manifest first so this layer is cacheable across code edits.
COPY pyproject.toml ./

# Resolve and build wheels for all runtime dependencies into /wheels.
RUN pip install --upgrade pip \
    && pip wheel --wheel-dir /wheels \
        "fastapi~=0.115.0" \
        "uvicorn[standard]~=0.32.0" \
        "pymodbus~=3.7.0" \
        "aiosqlite~=0.20.0" \
        "sqlalchemy[asyncio]~=2.0.0" \
        "openpyxl~=3.1.0" \
        "pydantic-settings~=2.5.0" \
        "jinja2~=3.1.0" \
        "pyyaml~=6.0.0"

# Fetch pinned frontend vendor assets (no CDN at runtime — served locally).
# Versions documented in README.md. Using curl with --fail so build breaks
# early on a missing release rather than shipping a broken image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /vendor/js /vendor/css \
    && curl -fsSL -o /vendor/js/htmx.min.js \
        "https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js" \
    && curl -fsSL -o /vendor/js/alpine.min.js \
        "https://cdn.jsdelivr.net/npm/alpinejs@3.14.8/dist/cdn.min.js" \
    && curl -fsSL -o /vendor/js/uplot.iife.min.js \
        "https://cdn.jsdelivr.net/npm/uplot@1.6.32/dist/uPlot.iife.min.js" \
    && curl -fsSL -o /vendor/css/uplot.min.css \
        "https://cdn.jsdelivr.net/npm/uplot@1.6.32/dist/uPlot.min.css" \
    && curl -fsSL -o /vendor/css/pico.min.css \
        "https://cdn.jsdelivr.net/npm/@picocss/pico@2.0.6/css/pico.min.css"


# ---- Stage 2: runtime ------------------------------------------------------
FROM --platform=linux/arm64 python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_HOME=/app \
    DATA_DIR=/data

# sqlite3 CLI is useful for the verification steps in CLAUDE.md.
# tini gives us a proper PID 1 for signal handling.
RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 tini \
    && rm -rf /var/lib/apt/lists/*

# Install pre-built wheels from the builder stage. No compiler in this image.
COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels /wheels/*.whl \
    && rm -rf /wheels

# Non-root runtime user.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 1000 app \
    && mkdir -p ${DATA_DIR} ${APP_HOME} \
    && chown -R app:app ${DATA_DIR} ${APP_HOME}

WORKDIR ${APP_HOME}

# Application code. devices.yaml is also bind-mounted by compose so edits
# don't require a rebuild, but we copy it here too for standalone runs.
COPY --chown=app:app app ./app
COPY --chown=app:app devices.yaml ./devices.yaml

# Frontend vendor assets built in the builder stage land under static/vendor.
COPY --from=builder --chown=app:app /vendor/js  ./app/static/vendor/js
COPY --from=builder --chown=app:app /vendor/css ./app/static/vendor/css

USER app

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "1", \
     "--loop", "uvloop", \
     "--http", "httptools"]
