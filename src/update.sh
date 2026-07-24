#!/bin/bash
# Compatibility launcher for one repository update.
# Usage: src/update.sh [ROOT] [-d DURATION] [-m MODE]

set -euo pipefail

invocation_directory=$(pwd -P) || exit 1
script_directory=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P) || exit 1
repository_directory=$(cd "$script_directory/.." && pwd -P) || exit 1
python_bin=${BKG_PYTHON:-}

[ -n "$python_bin" ] || [ ! -x "$repository_directory/.venv/bin/python" ] || \
	python_bin="$repository_directory/.venv/bin/python"
[ -n "$python_bin" ] || python_bin=python3
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$script_directory${PYTHONPATH:+:$PYTHONPATH}"
exec "$python_bin" -m bkg_py workflow-update \
	--invocation-directory "$invocation_directory" \
	"$@"
