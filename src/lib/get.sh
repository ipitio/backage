#!/bin/bash

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
python_bin=${BKG_PYTHON:-}

[ -n "$python_bin" ] || [ ! -x "$script_dir/../.venv/bin/python" ] || \
	python_bin="$script_dir/../.venv/bin/python"
[ -n "$python_bin" ] || python_bin=python3
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$script_dir${PYTHONPATH:+:$PYTHONPATH}"
exec "$python_bin" -m bkg_py select-owners "$@"
