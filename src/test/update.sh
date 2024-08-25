#!/bin/bash
# Test the sprinkler
# Usage: ./update.sh
# Copyright (c) ipitio
#
# shellcheck disable=SC1090,SC1091

if git ls-remote --exit-code origin index &>/dev/null; then
    if [ -d index ]; then
        pushd index || exit 1
        git pull
        popd || exit 1
    else
        git clone --depth 1 --branch index "$(git remote get-url origin)" index
    fi
fi

pushd "${0%/*}/.." || exit 1
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

popd || exit 1
git push origin "$(git subtree split --prefix index master)":index --force