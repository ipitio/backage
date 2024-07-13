#!/bin/bash
# Refresh json and README
# Usage: ./refre.sh
# Dependencies: jq, sqlite3
# Copyright (c) ipitio
#
# shellcheck disable=SC1091,SC2015

source lib.sh
sqlite3 "$BKG_INDEX_DB" "select distinct owner from '$BKG_INDEX_TBL_PKG';" | env_parallel -j 2000% --lb refresh_owner
