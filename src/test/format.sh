#!/bin/bash

set -euo pipefail

test_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd "$test_dir/../.." && pwd)
ruff_args=()

if [ "${1:-}" = "--check" ]; then
	ruff_args+=(--check)
	shift
fi

(($# == 0)) || {
	echo "Usage: $0 [--check]" >&2
	exit 2
}

if ! command -v uv >/dev/null 2>&1; then
	echo "Missing uv; install it from https://docs.astral.sh/uv/" >&2
	exit 1
fi

cd "$repo_dir"
uv sync --locked --quiet --no-install-project
uv run --locked --no-sync ruff format "${ruff_args[@]}" src
