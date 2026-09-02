# Multi-stage: build the dashboard with Node, run the API with Python.

FROM node:20-alpine AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


FROM python:3.11-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY cryptopulse/ ./cryptopulse/
# The `postgres` extra is not optional in a container that docker-compose points
# at Postgres: without psycopg the process starts and dies on the first write.
RUN pip install --no-cache-dir -e ".[postgres]"

COPY --from=frontend /build/dist ./frontend/dist

RUN mkdir -p /app/data && \
    adduser --disabled-password --gecos "" --uid 10001 cryptopulse && \
    chown -R cryptopulse:cryptopulse /app
USER cryptopulse

EXPOSE 8000

# /healthz, not /api/health: the latter always answers 200 because the dashboard
# needs its payload even when the feed is down. /healthz answers 503 once the
# radar has stopped scanning, which is the condition an orchestrator must act on
# — a process that is up but no longer scanning is not healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

CMD ["python", "-m", "cryptopulse.cli", "serve"]
