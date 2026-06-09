#!/bin/bash

set -euo pipefail

test_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd "$test_dir/../.." && pwd)
black_args=()

if [ "${1:-}" = "--check" ]; then
	black_args+=(--check)
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

mapfile -d '' -t python_files < <(
	cd "$repo_dir"
	find src -type f -name '*.py' -print0 | sort -z
)

((${#python_files[@]} > 0)) || {
	echo "No Python files found" >&2
	exit 1
}

cd "$repo_dir"
uv sync --locked --quiet
for file in "${python_files[@]}"; do
	uv run --locked --no-sync black "${black_args[@]}" "$file"
done
