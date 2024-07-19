#!/bin/bash
# Test the update function
# Usage: ./test.update.sh
# Copyright (c) ipitio
#
# shellcheck disable=SC1090,SC1091

cd "${0%/*}"/.. || exit 1 && source lib.sh
set -x
update_owners "$@"
[ "$(stat -c %s "$BKG_INDEX_SQL".zst)" -ge 1000 ] || exit 1 # db should not be empty
