#!/bin/bash
# Test the refresh function
# Usage: ./refre.sh
# Copyright (c) ipitio
#
# shellcheck disable=SC1090,SC1091

cd "${0%/*}"/.. || exit 1 && source lib.sh
refresh_owners "$@"

for json in "$BKG_INDEX_DIR"/*/*/*.json; do
    jq -e 'type == "object"' "$json" || exit 1 # json should not be empty
done