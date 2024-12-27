#!/bin/bash
# Test the sprinkler
# Usage: ./update.sh
# Copyright (c) ipitio
#
# shellcheck disable=SC1090,SC1091

root="${1:-.}"
[ "${root:0:1}" != "-" ] || root="."
pushd "$root"/src || exit 1
source bkg.sh
popd || exit 1
git config --global --add safe.directory "$(pwd)"
git config core.sharedRepository all
sudo chmod -R a+rwX . 2>/dev/null || chmod -R a+rwX .
sudo find . -type d -exec chmod g+s '{}' + 2>/dev/null || find . -type d -exec chmod g+s '{}' +

if git ls-remote --exit-code origin index &>/dev/null; then
    if [ -d index ]; then
        [ ! -d index.bak ] || rm -rf index.bak
        mv index index.bak
    fi

    git fetch origin index
    git worktree add index index
    pushd index || exit 1
    git reset --hard origin/index
    popd || exit 1
fi

[ -f index/.env ] && \cp index/.env src/env.env || echo "No .env file found"
pushd src || exit 1
main "${@:2}"

check_json() {
    if [ ! -s "$1" ]; then
        echo "Empty json: $1"
        rm -f "$1"
    else
        jq -e . <<<"$(cat "$1")" &>/dev/null || echo "Invalid json: $1"
    fi
}

check_xml() {
    if [ ! -s "$1" ]; then
        echo "Empty xml: $1"
        rm -f "$1"
    else
        xmllint --noout "$1" &>/dev/null || echo "Invalid xml: $1"
    fi
}

# db should not be empty, error if it is
[ "$(stat -c %s "$BKG_INDEX_SQL".zst)" -ge 1000 ] || exit 1
# json should be valid, warn if it is not
find .. -type f -name '*.json' | env_parallel check_json
# xml should be valid, warn if it is not
find .. -type f -name '*.xml' | env_parallel check_xml
popd || exit 1
\cp src/env.env index/.env

if git worktree list | grep -q index; then
    pushd index || exit 1
    git config --global user.name "${GITHUB_ACTOR}"
    git config --global user.email "${GITHUB_ACTOR}@users.noreply.github.com"
    git add .
    git commit -m "$(date -u +%Y-%m-%d)"
    git push
    popd || exit 1
fi

(git pull --rebase --autostash 2>/dev/null)
(git merge --abort 2>/dev/null)
(git pull --rebase --autostash -s ours &>/dev/null)
find . -type f -name '*.txt' -exec sed -i '/^<<<<<<<\|=======\|>>>>>>>/d' {} \;
git add -- *.txt README.md
git commit -m "$(date -u +%Y-%m-%d)"
git push
