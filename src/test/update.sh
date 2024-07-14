#!/bin/bash
# Test the update script
# Usage: ./test.update.sh
# Copyright (c) ipitio
#
# shellcheck disable=SC1090,SC1091

cd "${0%/*}" && cd .. || exit
source "${0##*/}"

# assert that the database is not empty after running the update script
[ "$(stat -c %s "$BKG_INDEX_SQL".zst)" -ge 1000 ] || exit 1
