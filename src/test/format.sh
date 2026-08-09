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

if ! command -v ruff >/dev/null 2>&1; then
	echo "Missing Ruff; run this command inside the bkg test image" >&2
	exit 1
fi

cd "$repo_dir"
ruff format "${ruff_args[@]}" src
