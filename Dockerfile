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
RUN pip install --no-cache-dir -e .

COPY --from=frontend /build/dist ./frontend/dist

RUN mkdir -p /app/data && \
    adduser --disabled-password --gecos "" --uid 10001 cryptopulse && \
    chown -R cryptopulse:cryptopulse /app
USER cryptopulse

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"

CMD ["python", "-m", "cryptopulse.cli", "serve"]
