ARG PYTHON_VERSION=3.15.0rc1
ARG UV_VERSION=0.12
ARG DOCKER_VERSION=29.1
FROM python:${PYTHON_VERSION}-slim-bookworm AS python-base

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv
FROM docker:${DOCKER_VERSION}-cli AS docker-cli

FROM python-base AS test

ARG DEBIAN_FRONTEND=noninteractive
COPY --from=uv /uv /usr/local/bin/uv
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
ENV PYRIGHT_PYTHON_CACHE_DIR=/opt/pyright-cache
ENV RUFF_CACHE_DIR=/tmp/bkg-ruff-cache
ENV UV_CACHE_DIR=/tmp/bkg-uv-cache
ENV UV_PROJECT_ENVIRONMENT=/opt/bkg-test
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --quiet --no-install-project
COPY . .
RUN bash src/test/regression.sh

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
    && rm -f /tmp/index.db
