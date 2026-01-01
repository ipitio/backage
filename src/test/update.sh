#!/bin/bash
# Test the sprinkler
# Usage: ./update.sh
# Copyright (c) ipitio
#
# shellcheck disable=SC1090,SC1091,SC2015,SC2034

root="$1"
[[ -n "$root" && ! "${root:0:2}" =~ -(m|d) ]] && shift || root="."
[ -d "$root" ] || mkdir -p "$root"
[ -d "$root/.git" ] || { gh auth status &>/dev/null && gh repo clone "${GITHUB_OWNER:-ipitio}/${GITHUB_REPO:-backage}" "$root"  -- --depth=1 -b "$GITHUB_BRANCH" --single-branch || git clone --depth=1 -b "$GITHUB_BRANCH" --single-branch "https://$([ -n "$GITHUB_TOKEN" ] && echo "$GITHUB_TOKEN@" || echo "")github.com/${GITHUB_OWNER:-ipitio}/${GITHUB_REPO:-backage}.git" "$root"; }

# actions: move db into root
shopt -s dotglob
[ ! -d .bkg ] || mv .bkg/* "$root"/
shopt -u dotglob

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

set -o allexport
BKG_BRANCH=$(git branch --show-current 2>/dev/null)
[ -n "$GITHUB_BRANCH" ] || GITHUB_BRANCH="$BKG_BRANCH"
BKG_INDEX=$([ "$GITHUB_BRANCH" = "master" ] && echo -n "index" || echo -n "index-${BKG_BRANCH:-}")
BKG_INDEX_DB=$BKG_ROOT/"$BKG_INDEX".db
BKG_INDEX_SQL=$BKG_ROOT/"$BKG_INDEX".sql
BKG_INDEX_DIR=$BKG_ROOT/"$BKG_INDEX"
set +o allexport

if git ls-remote --exit-code origin "$BKG_INDEX" &>/dev/null; then
    git worktree remove -f "$BKG_INDEX".bak &>/dev/null
    [ -d "$BKG_INDEX".bak ] || rm -rf "$BKG_INDEX".bak
    git worktree move "$BKG_INDEX" "$BKG_INDEX".bak &>/dev/null
    git fetch --depth=1 origin "$BKG_INDEX"
    git show-ref --verify --quiet "refs/remotes/origin/$BKG_INDEX" || git fetch origin "$BKG_INDEX:refs/remotes/origin/$BKG_INDEX"
    git branch --track -f "$BKG_INDEX" "origin/$BKG_INDEX" 2>/dev/null || git branch -f "$BKG_INDEX" "origin/$BKG_INDEX"
    BKG_IS_FIRST=true
else
    fd_list=$(find . -type f -o -type d | grep -vE "^\.($|\/(\.git\/*|.*\.md$))")
	git stash
    git switch --orphan "$BKG_INDEX"
    xargs rm -rf <<<"$fd_list"
    git add .
    git commit --allow-empty -m "init $BKG_INDEX"
    git push -u origin "$BKG_INDEX"
    git checkout "$([ -n "$GITHUB_BRANCH" ] && echo "$GITHUB_BRANCH" || echo "$BKG_BRANCH")"
	git stash pop || true
fi

git worktree remove -f "$BKG_INDEX" 2>/dev/null
git worktree add -f "$BKG_INDEX" "$BKG_INDEX"
[[ -d "$BKG_INDEX" || ! -d "$BKG_INDEX".bak ]] || git worktree move "$BKG_INDEX".bak "$BKG_INDEX"
pushd "$BKG_INDEX" || exit 1
git reset --hard origin/"$BKG_INDEX"
popd || exit 1
[ -f "$BKG_INDEX"/.env ] && \cp "$BKG_INDEX"/.env src/env.env || touch src/env.env
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
\cp src/env.env "$BKG_INDEX"/.env

if git worktree list | grep -q "$BKG_INDEX"; then
    pushd "$BKG_INDEX" || exit 1
    git add .
    git commit -m "$(date -u +%Y-%m-%d)"
    git push
    popd || exit 1
    ! git worktree list | grep -q "$BKG_INDEX".bak || git worktree remove -f "$BKG_INDEX".bak &>/dev/null
fi

(git pull --rebase --autostash 2>/dev/null)
(git merge --abort 2>/dev/null)
(git pull --rebase --autostash -s ours &>/dev/null)
find . -type f -name '*.txt' -exec sed -i '/^<<<<<<<\|=======\|>>>>>>>/d' {} \; 2>/dev/null
git add -- *.txt README.md
git commit -m "$(date -u +%Y-%m-%d)"
git push
popd || exit 1
