#!/bin/bash
# Test the refresh script
# Usage: ./test.refre.sh
# Copyright (c) ipitio
#
# shellcheck disable=SC1090,SC1091

cd "${0%/*}" && cd .. || exit
source "${0##*/}"

# assert that no json is empty after running the refresh script
for json in "$BKG_INDEX_DIR"/*.json; do
    jq -e 'length > 0' "$json" || exit 1
done
