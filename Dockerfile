ARG PYTHON_VERSION=3.14.6
ARG UV_VERSION=0.12
ARG DOCKER_VERSION=29.1
ARG NODE_VERSION=24
ARG NPM_VERSION=11.17.0
FROM python:${PYTHON_VERSION}-slim-bookworm AS python-base

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv
FROM docker:${DOCKER_VERSION}-cli AS docker-cli
FROM node:${NODE_VERSION}-bookworm-slim AS node-base

ARG NPM_VERSION
RUN npm install --global "npm@${NPM_VERSION}"

FROM node-base AS site-build

ENV ASTRO_TELEMETRY_DISABLED=1
WORKDIR /site
COPY site/package.json site/package-lock.json ./
RUN npm ci --strict-allow-scripts
COPY site/ ./
RUN npm run check && npm run build

FROM python-base AS test

ARG DEBIAN_FRONTEND=noninteractive
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=node-base /usr/local/ /usr/local/
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        git \
        libatomic1 \
        shellcheck \
        zstd \
    && rm -rf /var/lib/apt/lists/*
ENV PATH="/opt/bkg-test/bin:${PATH}"
ENV PYRIGHT_PYTHON_CACHE_DIR=/tmp/bkg-pyright-cache
ENV RUFF_CACHE_DIR=/tmp/bkg-ruff-cache
ENV UV_CACHE_DIR=/tmp/bkg-uv-cache
ENV UV_PROJECT_ENVIRONMENT=/opt/bkg-test
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --quiet --no-install-project
COPY --from=site-build /site/node_modules /opt/bkg-site-dev/node_modules
COPY --from=site-build /site/package.json /site/package-lock.json /opt/bkg-site-dev/
COPY --from=site-build /site/dist /opt/bkg-test/share/backage/site
COPY . .
RUN bash src/test/regression.sh \
    && rm -rf \
        /tmp/bkg-pyright-cache \
        /tmp/bkg-pytest-cache \
        /tmp/bkg-ruff-cache \
        /tmp/bkg-uv-cache

FROM python-base AS build

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/bkg_py ./src/bkg_py
RUN uv venv /opt/bkg --python /usr/local/bin/python \
    && uv export --quiet --locked --no-dev --no-emit-project \
        --format requirements.txt --output-file /tmp/runtime.txt \
    && uv pip install --python /opt/bkg/bin/python \
        --require-hashes --no-cache --requirements /tmp/runtime.txt \
    && uv pip install --python /opt/bkg/bin/python \
        --no-cache --no-deps .
COPY --from=site-build /site/dist /opt/bkg/share/backage/site

FROM python-base

ARG DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/bkg/bin:${PATH}"

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        zstd \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/bkg /opt/bkg
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY . .
RUN BKG_INDEX_DB=/tmp/index.db python -c \
        "from bkg_py.database import DatabaseRepository, DatabaseSettings; DatabaseRepository(DatabaseSettings.from_env()).ensure_schema()" \
    && python -c "import bkg_py, compression.zstd, httpx" \
    && docker --version \
    && ! command -v node \
    && rm -f /tmp/index.db
