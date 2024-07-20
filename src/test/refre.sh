#!/bin/bash
# Test the refresh function
# Usage: ./refre.sh
# Copyright (c) ipitio
#
# shellcheck disable=SC1090,SC1091,SC2317

cd "${0%/*}"/.. || exit 1 && source lib.sh
refresh_owners "$@"

check_json() {
    jq -e . <<<"$(cat "$1")" &>/dev/null || exit 1 # json should be valid
}

find index -type f -name '*.json' | env_parallel --halt now,fail=1 check_json
exit $?
