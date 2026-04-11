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

# Dependencies
COPY pyproject.toml ./
RUN pip install --upgrade pip \
    && pip install \
        "fastapi>=0.115.0" \
        "uvicorn[standard]>=0.32.0" \
        "httpx>=0.27.0" \
        "pydantic>=2.9.0" \
        "pydantic-settings>=2.5.0" \
        "jinja2>=3.1.4" \
        "python-multipart>=0.0.12" \
        "apscheduler>=3.10.4" \
        "structlog>=24.4.0" \
        "sqlite-vec>=0.1.3" \
        "numpy>=2.0.0" \
        "mcp[cli]>=1.20.0"

# App
COPY app ./app
COPY prompts ./prompts

# Persistent state
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8088/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8088"]
