#!/bin/bash
# Refresh json and README
# Usage: ./refre.sh
# Copyright (c) ipitio
#
# shellcheck disable=SC1091,SC2015

source lib.sh

main() {
    set_up
    sqlite3 "$BKG_INDEX_DB" "select distinct owner from '$BKG_INDEX_TBL_PKG';" | env_parallel --lb refresh_owner
}

main "$@"
