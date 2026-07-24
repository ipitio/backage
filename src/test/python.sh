#!/bin/bash

set -euo pipefail

test_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
src_dir=$(cd "$test_dir/.." && pwd)
repo_dir=$(cd "$src_dir/.." && pwd)

if ! command -v python3 >/dev/null 2>&1; then
	echo "Python tests require python3" >&2
	exit 1
fi

cd "$repo_dir"
uv sync --locked --quiet --no-install-project
PYTHONPATH="$src_dir" PYTHONDONTWRITEBYTECODE=1 \
	uv run --locked --no-sync pytest -q "$test_dir"
