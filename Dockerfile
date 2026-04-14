# Stage 1: Grab the Meilisearch binary from the official image
FROM getmeili/meilisearch:v1.13 AS meilisearch

# Stage 2: Application
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps (sqlite-vec wheel ist prebuilt, wir brauchen nur tini für saubere Signale)
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        tini \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Meilisearch binary (hybrid search sidecar)
COPY --from=meilisearch /bin/meilisearch /usr/local/bin/meilisearch

# Dependencies (pinned with constraints for supply-chain protection)
COPY pyproject.toml constraints.txt ./
RUN pip install --upgrade pip setuptools wheel \
    && pip install -c constraints.txt \
        "fastapi>=0.115.0,<=0.135.2" \
        "starlette<1.0.0" \
        "uvicorn[standard]>=0.32.0,<=0.42.0" \
        "httpx>=0.27.0" \
        "pydantic>=2.9.0,<=2.12.5" \
        "pydantic-settings>=2.5.0" \
        "jinja2>=3.1.4" \
        "python-multipart>=0.0.12,<=0.0.22" \
        "apscheduler>=3.10.4" \
        "structlog>=24.4.0" \
        "sqlite-vec>=0.1.3,<=0.1.7" \
        "mcp[cli]>=1.20.0,<=1.26.0" \
        "pymupdf>=1.24.0,<=1.27.2.2" \
        "meilisearch-python-sdk>=7.0.0,<=7.1.2"

# App
COPY app ./app
COPY prompts ./prompts
COPY entrypoint.sh ./

# Register console script entry point (deps already installed above)
RUN pip install --no-deps .

# Persistent state
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8088 3001

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8088/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["./entrypoint.sh"]
