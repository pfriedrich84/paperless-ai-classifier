FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps (sqlite-vec wheel ist prebuilt, wir brauchen nur tini für saubere Signale)
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies (pinned with constraints for supply-chain protection)
COPY pyproject.toml constraints.txt ./
RUN pip install --upgrade pip \
    && pip install -c constraints.txt \
        "fastapi>=0.115.0,<=0.135.2" \
        "uvicorn[standard]>=0.32.0,<=0.42.0" \
        "httpx>=0.27.0" \
        "pydantic>=2.9.0" \
        "pydantic-settings>=2.5.0" \
        "jinja2>=3.1.4" \
        "python-multipart>=0.0.12,<=0.0.22" \
        "apscheduler>=3.10.4" \
        "structlog>=24.4.0" \
        "sqlite-vec>=0.1.3,<=0.1.7" \
        "numpy>=2.0.0,<=2.4.4" \
        "mcp[cli]>=1.20.0,<=1.26.0"

# App
COPY app ./app
COPY prompts ./prompts
COPY entrypoint.sh ./

# Persistent state
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8088 3001

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8088/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["./entrypoint.sh"]
