#!/bin/bash
# Test the sprinkler
# Usage: src/update.sh
# Copyright (c) ipitio
#
# shellcheck disable=SC1090,SC1091,SC2015,SC2034

update_startup_phase_started_at() {
    date -u +%s
}

log_update_startup_phase() {
    local phase=$1
    local started_at=${2:-0}
    local elapsed=0

    ((started_at > 0)) || return 0
    elapsed=$(( $(date -u +%s) - started_at ))
    echo "Update setup phase '$phase' completed in ${elapsed}s"
}

root="$1"
[[ -n "$root" && ! "${root:0:2}" =~ -(m|d) ]] && shift || root="."
[ -d "$root" ] || mkdir -p "$root"
[ -n "${UPDATE_STARTUP_PHASE_STARTED_AT:-}" ] || UPDATE_STARTUP_PHASE_STARTED_AT=$(update_startup_phase_started_at)
[ -d "$root/.git" ] || { gh auth status &>/dev/null && gh repo clone "${GITHUB_OWNER:-ipitio}/${GITHUB_REPO:-backage}" "$root"  -- --depth=1 -b "$GITHUB_BRANCH" --single-branch || git clone --depth=1 -b "$GITHUB_BRANCH" --single-branch "https://github.com/${GITHUB_OWNER:-ipitio}/${GITHUB_REPO:-backage}.git" "$root"; }
log_update_startup_phase "ensure-root-repo" "$UPDATE_STARTUP_PHASE_STARTED_AT"

# actions: move db into root
shopt -s dotglob
[ ! -d .bkg ] || mv .bkg/* "$root"/
shopt -u dotglob

pushd "$root" || exit 1
pushd src || exit 1
source bkg.sh
popd || exit 1

# permissions
[ -n "$GITHUB_TOKEN" ] || GITHUB_TOKEN=$(remote_url=$(git config --get remote.origin.url); if grep -q '@' <<<"$remote_url"; then grep -oP '(?<=://)[^@]+' <<<"$remote_url"; else echo ""; fi)
[ -n "$GITHUB_TOKEN" ] || ! gh auth status &>/dev/null || GITHUB_TOKEN=$(gh auth token)
[ -n "$GITHUB_ACTOR" ] || GITHUB_ACTOR="${GITHUB_OWNER:-ipitio}"
git config user.name "${GITHUB_ACTOR}"
git config user.email "${GITHUB_ACTOR}@users.noreply.github.com"
git config credential.helper "!f() { echo username=${GITHUB_ACTOR}; echo password=${GITHUB_TOKEN}; }; f"
git config --add safe.directory "$(pwd)"
git config core.sharedRepository all
git config remote.origin.promisor true
git config remote.origin.partialclonefilter blob:none
git config extensions.partialClone origin

# performance
if git fsmonitor--daemon status >/dev/null 2>&1 || git fsmonitor--daemon start >/dev/null 2>&1; then
    git config core.fsmonitor true
else
    git config --unset-all core.fsmonitor >/dev/null 2>&1 || :
fi
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
BKG_INDEX_DB_ZST="$BKG_INDEX_DB".zst
BKG_INDEX_SQL=$BKG_ROOT/"$BKG_INDEX".sql
BKG_INDEX_DIR=$BKG_ROOT/"$BKG_INDEX"
set +o allexport

UPDATE_STARTUP_PHASE_STARTED_AT=$(update_startup_phase_started_at)
WORKTREE_PHASE_STARTED_AT=$(update_startup_phase_started_at)

if git ls-remote --exit-code origin "$BKG_INDEX" &>/dev/null; then
	log_update_startup_phase "check-index-branch" "$WORKTREE_PHASE_STARTED_AT"
	WORKTREE_PHASE_STARTED_AT=$(update_startup_phase_started_at)
    git worktree remove -f "$BKG_INDEX".bak &>/dev/null
    [ -d "$BKG_INDEX".bak ] || rm -rf "$BKG_INDEX".bak
    git worktree move "$BKG_INDEX" "$BKG_INDEX".bak &>/dev/null
    git fetch --depth=1 --filter=blob:none origin "$BKG_INDEX"
    git show-ref --verify --quiet "refs/remotes/origin/$BKG_INDEX" || git fetch --filter=blob:none origin "$BKG_INDEX:refs/remotes/origin/$BKG_INDEX"
    git branch --track -f "$BKG_INDEX" "origin/$BKG_INDEX" 2>/dev/null || git branch -f "$BKG_INDEX" "origin/$BKG_INDEX"
    BKG_IS_FIRST=true
	log_update_startup_phase "prepare-index-branch-ref" "$WORKTREE_PHASE_STARTED_AT"
else
	log_update_startup_phase "check-index-branch" "$WORKTREE_PHASE_STARTED_AT"
	WORKTREE_PHASE_STARTED_AT=$(update_startup_phase_started_at)
    fd_list=$(find . -type f -o -type d | grep -vE "^\.($|\/(\.git\/*|.*\.md$))")
	git stash
    git switch --orphan "$BKG_INDEX"
    xargs rm -rf <<<"$fd_list"
    git add .
    git commit --allow-empty -m "init $BKG_INDEX"
    git push -u origin "$BKG_INDEX"
    git checkout "$([ -n "$GITHUB_BRANCH" ] && echo "$GITHUB_BRANCH" || echo "$BKG_BRANCH")"
	git stash pop || true
	log_update_startup_phase "create-index-branch" "$WORKTREE_PHASE_STARTED_AT"
fi

WORKTREE_PHASE_STARTED_AT=$(update_startup_phase_started_at)
git worktree remove -f "$BKG_INDEX" 2>/dev/null
git worktree add --no-checkout -f "$BKG_INDEX" "$BKG_INDEX"
[[ -d "$BKG_INDEX" || ! -d "$BKG_INDEX".bak ]] || git worktree move "$BKG_INDEX".bak "$BKG_INDEX"
log_update_startup_phase "attach-index-worktree" "$WORKTREE_PHASE_STARTED_AT"

WORKTREE_PHASE_STARTED_AT=$(update_startup_phase_started_at)
pushd "$BKG_INDEX" || exit 1
index_sparse_set_root
git reset --hard origin/"$BKG_INDEX"
popd || exit 1
ensure_pages_dotfiles_visible "$BKG_INDEX"
[ -f "$BKG_INDEX"/.env ] && \cp "$BKG_INDEX"/.env src/env.env || touch src/env.env
pushd src || exit 1
log_update_startup_phase "prepare-index-worktree" "$UPDATE_STARTUP_PHASE_STARTED_AT"

if [ ! -f "$BKG_INDEX_DB_ZST" ] && [ ! -f "$BKG_INDEX_SQL".zst ] && [ ! -f "$BKG_INDEX_DB" ]; then
    UPDATE_STARTUP_PHASE_STARTED_AT=$(update_startup_phase_started_at)
    dldb >/dev/null 2>&1 || true
    log_update_startup_phase "download-initial-db" "$UPDATE_STARTUP_PHASE_STARTED_AT"
fi

db_size=$(stat -c %s "$BKG_INDEX_DB_ZST" 2>/dev/null || stat -c %s "$BKG_INDEX_SQL".zst 2>/dev/null || stat -c %s "$BKG_INDEX_DB" 2>/dev/null || echo 0)
num_owner_db=$(sqlite3 "$BKG_INDEX_DB" "SELECT COUNT(DISTINCT owner) FROM $BKG_INDEX_TBL_PKG")
num_owner_index=$(index_top_level_owner_count)

if [ "$GITHUB_OWNER" = "ipitio" ] && ((num_owner_db < num_owner_index/2)) && ((db_size < 100000)); then
    [ ! -f "$BKG_INDEX_DB".bak ] || mv "$BKG_INDEX_DB".bak "$BKG_INDEX_DB"
    echo "Failed to download the latest database"
    check_db
    exit 1
fi

main "$@"
return_code=$?
# db should not be empty, error if it is
[ "$(stat -c %s "$BKG_INDEX_DB_ZST" 2>/dev/null || stat -c %s "$BKG_INDEX_SQL".zst 2>/dev/null || echo 0)" -ge 100 ] || exit 1
# files should be valid, warn if not, unless only opted out owners
#(( return_code == 1 )) || find .. -type f -name '*.json' -o -name '*.xml' | parallel --lb src/index.sh {}
popd || exit 1
\cp src/env.env "$BKG_INDEX"/.env

if git worktree list | grep -q "$BKG_INDEX"; then
    pushd "$BKG_INDEX" || exit 1
    git add .
    git commit -m "$(date -u +%Y-%m-%d)"
    git push --set-upstream origin "$BKG_INDEX"
    popd || exit 1
    ! git worktree list | grep -q "$BKG_INDEX".bak || git worktree remove -f "$BKG_INDEX".bak &>/dev/null
fi

(git pull --rebase --autostash 2>/dev/null)
(git merge --abort 2>/dev/null)
(git pull --rebase --autostash -s ours &>/dev/null)
find . -type f -name '*.txt' -exec sed -i '/^<<<<<<<\|=======\|>>>>>>>/d' {} \; 2>/dev/null
git add -- *.txt README.md 2>/dev/null || git add README.md 2>/dev/null || true

if ! git diff --cached --quiet; then
    git commit -m "$(date -u +%Y-%m-%d)"
    git push
else
    echo "No top-level txt/README changes to commit"
fi
popd || exit 1
