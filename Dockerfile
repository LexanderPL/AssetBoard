# syntax=docker/dockerfile:1.7

FROM python:3.14-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83 AS base

FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM --platform=$BUILDPLATFORM python:3.14-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83 AS assets

WORKDIR /build

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl make \
    && rm -rf /var/lib/apt/lists/*

COPY . .
RUN make css

FROM base AS builder

COPY --from=uv /uv /uvx /bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-default-groups --no-install-project

COPY . .
COPY --from=assets /build/src/assetboard/static/css/app.css src/assetboard/static/css/app.css

# The wheel's build hook (hatch_build.py) compiles the gettext .mo catalogs from the committed
# .po files, so they ship in the wheel and the runtime image; fail the build if they do not.
RUN uv build --wheel --out-dir /wheels \
    && uv pip install --python /opt/venv/bin/python --no-deps /wheels/*.whl \
    && /opt/venv/bin/python -c "from importlib.resources import files; \
locales = files('assetboard') / 'locales'; \
missing = [lang for lang in ('en', 'pl') if not (locales / lang / 'LC_MESSAGES' / 'messages.mo').is_file()]; \
assert not missing, f'compiled gettext catalogs missing from the image: {missing}'"

FROM base AS runtime

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

RUN mkdir -p /data /licenses/AssetBoard/vendor \
    && chown 10001:10001 /data

COPY --from=builder --chown=10001:10001 /opt/venv /opt/venv
COPY --from=builder /build/LICENSE /licenses/AssetBoard/LICENSE
COPY --from=builder /build/src/assetboard/data/NOTICE /licenses/AssetBoard/NOTICE
COPY --from=builder /build/src/assetboard/data/SECLists-LICENSE.txt /licenses/AssetBoard/SECLists-LICENSE.txt
COPY --from=builder /build/src/assetboard/static/vendor/*-LICENSE.txt /licenses/AssetBoard/vendor/
COPY --from=builder /build/src/assetboard/static/vendor/ECHARTS-NOTICE.txt /licenses/AssetBoard/vendor/

VOLUME ["/data"]
EXPOSE 8000
USER 10001:10001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

CMD ["assetboard", "serve", "--host", "0.0.0.0", "--port", "8000"]
