#!/bin/bash
# Test the sprinkler
# Usage: ./update.sh
# Copyright (c) ipitio
#
# shellcheck disable=SC1090,SC1091

check_json() {
    [ -s "$1" ] || exit 1 # json should not be empty
    jq -e . <<<"$(cat "$1")" &>/dev/null || exit 1 # json should be valid
}

cd "${0%/*}"/.. || exit 1
source bkg.sh
main "$@"

# db should not be empty
[ "$(stat -c %s "$BKG_INDEX_SQL".zst)" -ge 1000 ] || exit 1

# json should be valid
find .. -type f -name '*.json' | env_parallel --halt now,fail=1 check_json || exit 1
