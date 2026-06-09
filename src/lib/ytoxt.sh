#!/bin/bash

script_dir=$(
	cd -P "$(dirname "${BASH_SOURCE[0]}")" || exit
	pwd -P
) || exit 1
script_dir=${script_dir%/*}
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$script_dir${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m bkg_py publish "${1:-}"
