# syntax=docker/dockerfile:1.7

ARG PYTHON_BASE=python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

FROM ghcr.io/astral-sh/uv:0.8.22 AS uv

FROM ${PYTHON_BASE} AS builder

COPY --from=uv /uv /uvx /bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl make \
    && rm -rf /var/lib/apt/lists/*

COPY . .

RUN uv sync --frozen --no-default-groups --no-install-project \
    && make css i18n-compile \
    && uv build --wheel --out-dir /wheels \
    && uv pip install --python /opt/venv/bin/python --no-deps /wheels/*.whl

FROM ${PYTHON_BASE} AS runtime

ARG VERSION=0.0.0
ARG SOURCE_URL
ARG REVISION

LABEL org.opencontainers.image.title="AssetBoard" \
      org.opencontainers.image.description="A self-hosted personal finance web app" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${REVISION}"

ENV PATH="/opt/venv/bin:${PATH}" \
    ASSETBOARD_DATA_DIR=/data \
    ASSETBOARD_ENV=prod \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/tmp

WORKDIR /app

RUN mkdir -p /data \
    && chown 10001:10001 /data

COPY --from=builder --chown=10001:10001 /opt/venv /opt/venv

VOLUME ["/data"]
EXPOSE 8000
USER 10001:10001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

CMD ["assetboard", "serve", "--host", "0.0.0.0", "--port", "8000"]
