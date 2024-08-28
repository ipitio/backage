#!/bin/bash
# Test the sprinkler
# Usage: ./update.sh
# Copyright (c) ipitio
#
# shellcheck disable=SC1090,SC1091

if git ls-remote --exit-code origin index &>/dev/null; then
    git fetch origin index
    git worktree add index-branch index
    pushd index-branch || exit 1
    git pull
    popd || exit 1
    find . -type d -exec sh -c 'mkdir -vp index-branch/"$1"' _ {} \;
    find . -type f -exec sh -c 'mkdir -p index-branch/"$(dirname "$1")" && mv -nv "$1" index-branch/"$1"' _ {} \;
    git worktree remove -f index-branch
fi

pushd "${0%/*}/.." || exit 1
source bkg.sh
main "$@"

check_json() {
    if [ ! -s "$1" ]; then
        echo "Empty json: $1"
        rm -f "$1"
    else
        jq -e . <<<"$(cat "$1")" &>/dev/null || echo "Invalid json: $1"
    fi
}

# db should not be empty, error if it is
[ "$(stat -c %s "$BKG_INDEX_SQL".zst)" -ge 1000 ] || exit 1
# json should be valid, warn if it is not
find .. -type f -name '*.json' | env_parallel check_json
popd || exit 1

git config --global user.name "${GITHUB_ACTOR}"
git config --global user.email "${GITHUB_ACTOR}@users.noreply.github.com"
git worktree add index-branch index
pushd index-branch || exit 1
git pull
pushd ../index || exit 1
find . -type d -exec sh -c 'mkdir -vp ../index-branch/"$1"' _ {} \;
find . -type f -exec sh -c 'mkdir -p ../index-branch/"$(dirname "$1")" && mv -nv "$1" ../index-branch/"$1"' _ {} \;
popd || exit 1
git add .
git commit -m "hydration"
git push -f
popd || exit 1
git worktree remove -f index-branch
