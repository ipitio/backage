#!/bin/bash
# Test the sprinkler
# Usage: ./update.sh
# Copyright (c) ipitio
#
# shellcheck disable=SC1090,SC1091

if git ls-remote --exit-code origin index &>/dev/null; then
    git worktree add index-branch index
    \cp -r index-branch/* index/
    pushd index || exit 1
    git pull
    popd || exit 1
    git worktree remove index-branch
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

git worktree add index-branch index
\cp -r index/* index-branch/
pushd index-branch || exit 1
git add .
git commit -m "hydration"
git push
popd || exit 1
git worktree remove index-branch
