#!/bin/bash

set -euo pipefail

test_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd "$test_dir/../.." && pwd)

cd "$repo_dir"
bash "$test_dir/format.sh" --check
uv run --locked --no-sync ruff check src/bkg_py src/test/test_*.py
uv run --locked --no-sync pylint src/bkg_py src/test/test_*.py
uv run --locked --no-sync pyright
