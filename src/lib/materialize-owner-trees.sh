#!/bin/bash
# Materialize queued owner paths through the current sparse-worktree helpers.
# shellcheck disable=SC1091

set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$script_dir/.."
source lib/util.sh

(($# > 0)) || exit 0
printf '%s\n' "$@" | index_sparse_add_paths
