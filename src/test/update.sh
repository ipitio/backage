#!/bin/bash
# Test the sprinkler
# Usage: ./update.sh
# Copyright (c) ipitio
#
# shellcheck disable=SC1090,SC1091

cd "${0%/*}"/.. || exit 1
source bkg.sh
main "$@"

check_json() {
    [ -s "$1" ] || echo "Empty json: $1"
    jq -e . <<<"$(cat "$1")" &>/dev/null || echo "Invalid json: $1"
}

# db should not be empty, error if it is
[ "$(stat -c %s "$BKG_INDEX_SQL".zst)" -ge 1000 ] || exit 1

# json should be valid, warn if it is not
find .. -type f -name '*.json' | env_parallel check_json
