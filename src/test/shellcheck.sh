#!/bin/bash

set -euo pipefail

test_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
src_dir=$(cd "$test_dir/.." && pwd)

if ! command -v shellcheck >/dev/null 2>&1; then
	echo "Shell checks require shellcheck" >&2
	exit 1
fi

find "$src_dir" -type f -name '*.sh' -print0 | xargs -0 shellcheck
