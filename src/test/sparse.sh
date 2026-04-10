#!/bin/bash

# shellcheck disable=SC2034

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
src_dir=${src_dir:?}
workdir=${workdir:?}

setup_index_repo() {
	local repo_path=$1

	mkdir -p "$repo_path"
	git -C "$repo_path" init -q
	git -C "$repo_path" config user.name test
	git -C "$repo_path" config user.email test@example.com
	mkdir -p "$repo_path/alpha/repo-a" "$repo_path/beta/repo-b"
	printf '%s\n' 'env' >"$repo_path/.env"
	printf '%s\n' 'readme' >"$repo_path/README.md"
	printf '%s\n' '{}' >"$repo_path/alpha/repo-a/package.json"
	printf '%s\n' '{}' >"$repo_path/beta/repo-b/package.json"
	git -C "$repo_path" add .
	git -C "$repo_path" commit -qm init
}

test_index_top_level_owner_count_uses_git_tree_with_root_sparse_checkout() {
	local repo_path="$workdir/index-root-sparse"
	local owner_count

	setup_index_repo "$repo_path"
	BKG_INDEX_DIR="$repo_path"
	index_sparse_set_root || fail "Expected sparse root initialization to succeed"

	assert_file_exists "$repo_path/.env"
	assert_file_exists "$repo_path/README.md"
	[ ! -d "$repo_path/alpha" ] || fail "Expected root sparse checkout to omit owner directories"
	[ ! -d "$repo_path/beta" ] || fail "Expected root sparse checkout to omit owner directories"

	owner_count=$(index_top_level_owner_count)
	[ "$owner_count" = "2" ] || fail "Expected git tree owner count to remain available without materializing owner directories"
}

test_materialize_index_queue_owners_adds_owner_subtrees() {
	local repo_path="$workdir/index-queue-materialize"

	setup_index_repo "$repo_path"
	BKG_INDEX_DIR="$repo_path"
	BKG_ENV="$workdir/sparse.env"
	: >"$BKG_ENV"
	index_sparse_set_root || fail "Expected sparse root initialization to succeed before owner materialization"
	set_BKG BKG_OWNERS_QUEUE '1/alpha\n2/beta'

	materialize_index_queue_owners || fail "Expected queued owners to be materialized into the sparse index worktree"

	[ -d "$repo_path/alpha" ] || fail "Expected queued owner alpha to be materialized"
	[ -d "$repo_path/beta" ] || fail "Expected queued owner beta to be materialized"
	assert_file_exists "$repo_path/alpha/repo-a/package.json"
	assert_file_exists "$repo_path/beta/repo-b/package.json"
}

trap cleanup EXIT

pushd "$src_dir" >/dev/null
export BKG_SKIP_DEP_VERIFY=1
source lib/util.sh
popd >/dev/null

test_index_top_level_owner_count_uses_git_tree_with_root_sparse_checkout
test_materialize_index_queue_owners_adds_owner_subtrees

echo "Sparse workflow regression tests passed"