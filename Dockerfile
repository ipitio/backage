FROM ghcr.io/astral-sh/uv:0.11.19 AS uv

FROM python:3.14.6-slim-bookworm AS build

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

FROM python:3.14.6-slim-bookworm

ARG DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/bkg/bin:${PATH}"

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        git \
        jq \
        libxml2-utils \
        parallel \
        sqlite3 \
        zstd \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/bkg /opt/bkg
COPY . .
RUN BKG_ROOT=/app BKG_INDEX_DB=/tmp/index.db bkg database ensure-schema \
    && python -c "import bkg_py, compression.zstd, httpx" \
    && rm -f /tmp/index.db
