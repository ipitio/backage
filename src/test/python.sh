#!/bin/bash

set -euo pipefail

test_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
src_dir=$(cd "$test_dir/.." && pwd)
repo_dir=$(cd "$src_dir/.." && pwd)

if ! command -v pytest >/dev/null 2>&1; then
	echo "Missing pytest; run this command inside the bkg test image" >&2
	exit 1
fi

cd "$repo_dir"
PYTHONPATH="$src_dir" PYTHONDONTWRITEBYTECODE=1 \
	pytest -q -o cache_dir=/tmp/bkg-pytest-cache "$test_dir"
