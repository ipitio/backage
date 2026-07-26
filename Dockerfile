ARG PYTHON_VERSION=3.14
ARG UV_VERSION=0.11
FROM python:${PYTHON_VERSION}-slim-bookworm AS python-base

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

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
COPY . .
RUN BKG_ROOT=/app BKG_INDEX_DB=/tmp/index.db bkg database ensure-schema \
    && python -c "import bkg_py, compression.zstd, httpx" \
    && rm -f /tmp/index.db
