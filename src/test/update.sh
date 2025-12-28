#!/bin/bash
# Test the sprinkler
# Usage: ./update.sh
# Copyright (c) ipitio
#
# shellcheck disable=SC1090,SC1091,SC2015,SC2034

root="$1"
[[ -n "$root" && ! "${root:0:2}" =~ -(m|d) ]] && shift || root="."
[ -d "$root" ] || { gh auth status &>/dev/null && gh repo clone "${GITHUB_OWNER:-ipitio}/${GITHUB_REPO:-backage}" "$root"  -- --depth=1 -b "$GITHUB_BRANCH" --single-branch || git clone --depth=1 -b "$GITHUB_BRANCH" --single-branch "https://$([ -n "$GITHUB_TOKEN" ] && echo "$GITHUB_TOKEN@" || echo "")github.com/${GITHUB_OWNER:-ipitio}/${GITHUB_REPO:-backage}.git" "$root"; }
pushd "$root" || exit 1
pushd src || exit 1
source bkg.sh
popd || exit 1

# permissions
[ -n "$GITHUB_TOKEN" ] || GITHUB_TOKEN=$(if git config --get remote.origin.url | grep -q '@'; then grep -oP '(?<=://)[^@]+'; else echo ""; fi)
[ -n "$GITHUB_TOKEN" ] || ! gh auth status &>/dev/null || GITHUB_TOKEN=$(gh auth token)
[ -n "$GITHUB_ACTOR" ] || GITHUB_ACTOR="${GITHUB_OWNER:-ipitio}"
git config user.name "${GITHUB_ACTOR}"
git config user.email "${GITHUB_ACTOR}@users.noreply.github.com"
git config --get-regexp --name-only '^url\.https://.+\.insteadof' | xargs -n1 git config --unset-all 2>/dev/null
git config url.https://"${GITHUB_TOKEN}"@github.com/.insteadOf https://github.com/
git config --add safe.directory "$(pwd)"
git config core.sharedRepository all

# performance
git config core.fsmonitor true
git config core.untrackedcache true
git config feature.manyFiles true
git update-index --index-version 4

sudonot chmod -R a+rwX .
sudonot find . -type d -exec chmod g+s '{}' +

if git ls-remote --exit-code origin index &>/dev/null; then
    git worktree remove -f index.bak &>/dev/null
    [ -d index.bak ] || rm -rf index.bak
    git worktree move index index.bak &>/dev/null
    git fetch origin index
    BKG_IS_FIRST=true
else
    fd_list=$(find . -type f -o -type d | grep -vE "^\.($|\/(\.git\/*|.*\.md$))")
    git switch --orphan index
    xargs rm -rf <<<"$fd_list"
    git add .
    git commit --allow-empty -m "init index"
    git push -u origin index
    git checkout master
fi

git worktree remove -f index 2>/dev/null
git worktree add -f index index
[[ -d index || ! -d index.bak ]] || git worktree move index.bak index
pushd index || exit 1
git reset --hard origin/index
popd || exit 1
[ -f index/.env ] && \cp index/.env src/env.env || touch src/env.env
pushd src || exit 1

db_size=$(stat -c %s "$BKG_INDEX_SQL".zst)
num_owner_db=$(sqlite3 "$BKG_INDEX_DB" "SELECT COUNT(DISTINCT owner) FROM $BKG_INDEX_TBL_PKG")
num_owner_index=$(find "$BKG_INDEX_DIR" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; 2>/dev/null | sort -u | awk '{print $1}' | wc -l)

if [ "$GITHUB_OWNER" = "ipitio" ] && ((num_owner_db < num_owner_index/2)) && ((db_size < 100000)); then
    [ ! -f "$BKG_INDEX_DB".bak ] || mv "$BKG_INDEX_DB".bak "$BKG_INDEX_DB"
    echo "Failed to download the latest database"
    check_db
    exit 1
fi

main "$@"
return_code=$?
# db should not be empty, error if it is
[ "$(stat -c %s "$BKG_INDEX_SQL".zst)" -ge 100 ] || exit 1
# files should be valid, warn if not, unless only opted out owners
#(( return_code == 1 )) || find .. -type f -name '*.json' -o -name '*.xml' | parallel --lb test/index.sh {}
popd || exit 1
\cp src/env.env index/.env

if git worktree list | grep -q index; then
    pushd index || exit 1
    git add .
    git commit -m "$(date -u +%Y-%m-%d)"
    git push
    popd || exit 1
    ! git worktree list | grep -q index.bak || git worktree remove -f index.bak &>/dev/null
fi

(git pull --rebase --autostash 2>/dev/null)
(git merge --abort 2>/dev/null)
(git pull --rebase --autostash -s ours &>/dev/null)
find . -type f -name '*.txt' -exec sed -i '/^<<<<<<<\|=======\|>>>>>>>/d' {} \; 2>/dev/null
git add -- *.txt README.md
git commit -m "$(date -u +%Y-%m-%d)"
git push
popd || exit 1
