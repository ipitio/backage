#!/bin/bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
src_dir=${src_dir:?}
workdir=${workdir:?}

assert_json_array() {
	[ "$(jq -r 'type' "$1")" = "array" ] || fail "Expected array root in $1"
}

assert_json_length() {
	local actual
	actual=$(jq 'length' "$1")
	[ "$actual" -eq "$2" ] || fail "Expected $1 to contain $2 items, got $actual"
}

assert_repo_only() {
	jq -e --arg repo "$2" 'all(.[]; .repo == $repo)' "$1" >/dev/null || fail "Expected $1 to contain only repo $2"
}

assert_size_lt() {
	local size
	size=$(stat -c %s "$1")
	[ "$size" -lt "$2" ] || fail "Expected $1 to be smaller than $2 bytes, got $size"
}

assert_size_gt() {
	local size
	size=$(stat -c %s "$1")
	[ "$size" -gt "$2" ] || fail "Expected $1 to be larger than $2 bytes, got $size"
}

version_total() {
	jq '[ .[] | ((.version // []) | length) ] | add' "$1"
}

write_package_json() {
	local file=$1
	local owner=$2
	local repo=$3
	local package=$4
	local version_count=$5
	local payload_file=$6

	jq -nc \
		--arg owner "$owner" \
		--arg repo "$repo" \
		--arg package "$package" \
		--arg date "2026-03-30" \
		--argjson version_count "$version_count" \
		--rawfile payload "$payload_file" '
		{
			owner: $owner,
			repo: $repo,
			package: $package,
			downloads: "1",
			raw_downloads: 1,
			date: $date,
			version: [
				range(0; $version_count) | {
					id: (100000 + .),
					name: ("v" + (. | tostring)),
					tags: [if . == ($version_count - 1) then "latest" else ("tag-" + (. | tostring)) end],
					downloads: "1",
					raw_downloads: 1,
					date: $date,
					notes: $payload
				}
			]
		}' >"$file"
}

build_owner_arrays() {
	local owner_dir=$1
	local json_file
	local -a json_files=()
	local repo

	find "$owner_dir" -type f \( -name '*.json.tmp' -o -name '*.json.abs' -o -name '*.json.rel' \) -delete
	find "$owner_dir" -regextype posix-extended -type f -regex '.*\.json\.[[:alnum:]]{6}$' -delete
	find "$owner_dir" -type d -name '*.d' -prune -exec rm -rf {} +
	mapfile -d '' -t json_files < <(find "$owner_dir" -type d -name '*.d' -prune -o -type f -name '*.json' ! -name '.*' -print0 | LC_ALL=C sort -z)
	if ((${#json_files[@]} == 0)); then
		printf '[]\n' >"$owner_dir/.json.tmp"
	else
		for json_file in "${json_files[@]}"; do
			cat "$json_file"
			printf '\n'
		done | jq -cs '.' >"$owner_dir/.json.tmp"
	fi
	mv -f "$owner_dir/.json.tmp" "$owner_dir/.json"
	bash "$src_dir/lib/ytoxt.sh" "$owner_dir/.json" >/dev/null

	while IFS= read -r repo; do
		[ -n "$repo" ] || continue
		jq -c --arg repo "$repo" '[.[] | select(.repo == $repo)]' "$owner_dir/.json" >"$owner_dir/$repo/.json.tmp"
		mv -f "$owner_dir/$repo/.json.tmp" "$owner_dir/$repo/.json"
		bash "$src_dir/lib/ytoxt.sh" "$owner_dir/$repo/.json" >/dev/null
	done < <(find "$owner_dir" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort)
}

test_small_owner_and_repo_arrays() {
	local owner_dir="$workdir/small/Lazztech"
	local empty_payload="$workdir/empty.txt"

	: >"$empty_payload"
	mkdir -p "$owner_dir/Libre-Closet" "$owner_dir/SideRepo"

	write_package_json "$owner_dir/Libre-Closet/libre-closet.json" "Lazztech" "Libre-Closet" "libre-closet" 3 "$empty_payload"
	write_package_json "$owner_dir/Libre-Closet/libre-closet-dev.json" "Lazztech" "Libre-Closet" "libre-closet-dev" 2 "$empty_payload"
	write_package_json "$owner_dir/SideRepo/sidecar.json" "Lazztech" "SideRepo" "sidecar" 1 "$empty_payload"

	build_owner_arrays "$owner_dir"

	assert_file_exists "$owner_dir/.json"
	assert_file_exists "$owner_dir/.xml"
	assert_json_array "$owner_dir/.json"
	assert_json_length "$owner_dir/.json" 3
	assert_size_lt "$owner_dir/.json" 50000000
	assert_size_lt "$owner_dir/.xml" 50000000
	assert_contains "$owner_dir/.xml" "libre-closet"
	assert_contains "$owner_dir/.xml" "sidecar"

	assert_file_exists "$owner_dir/Libre-Closet/.json"
	assert_file_exists "$owner_dir/Libre-Closet/.xml"
	assert_json_array "$owner_dir/Libre-Closet/.json"
	assert_json_length "$owner_dir/Libre-Closet/.json" 2
	assert_repo_only "$owner_dir/Libre-Closet/.json" "Libre-Closet"
	assert_contains "$owner_dir/Libre-Closet/.xml" "libre-closet"
	assert_not_contains "$owner_dir/Libre-Closet/.xml" "sidecar"

	assert_file_exists "$owner_dir/SideRepo/.json"
	assert_file_exists "$owner_dir/SideRepo/.xml"
	assert_json_array "$owner_dir/SideRepo/.json"
	assert_json_length "$owner_dir/SideRepo/.json" 1
	assert_repo_only "$owner_dir/SideRepo/.json" "SideRepo"
	assert_contains "$owner_dir/SideRepo/.xml" "sidecar"
	assert_not_contains "$owner_dir/SideRepo/.xml" "libre-closet"
}

test_owner_arrays_cleanup_legacy_version_dirs() {
	local owner_dir="$workdir/legacy/Lazztech"
	local empty_payload="$workdir/empty-legacy.txt"
	local legacy_dir="$owner_dir/Libre-Closet/libre-closet.d"

	: >"$empty_payload"
	mkdir -p "$owner_dir/Libre-Closet" "$legacy_dir"

	write_package_json "$owner_dir/Libre-Closet/libre-closet.json" "Lazztech" "Libre-Closet" "libre-closet" 2 "$empty_payload"
	printf '%s\n' '{"id":999,"name":"legacy-version"}' >"$legacy_dir/legacy.json"

	build_owner_arrays "$owner_dir"

	[ ! -d "$legacy_dir" ] || fail "Expected owner array creation to remove legacy package.d directories"
	assert_json_array "$owner_dir/.json"
	assert_json_length "$owner_dir/.json" 1
}

test_owner_arrays_cleanup_stale_json_sidecars() {
	local owner_dir="$workdir/sidecars/Lazztech"
	local empty_payload="$workdir/empty-sidecars.txt"

	: >"$empty_payload"
	mkdir -p "$owner_dir/Libre-Closet"

	write_package_json "$owner_dir/Libre-Closet/libre-closet.json" "Lazztech" "Libre-Closet" "libre-closet" 1 "$empty_payload"
	printf '%s\n' 'stale owner tmp' >"$owner_dir/owner.json.tmp"
	printf '%s\n' 'stale repo abs' >"$owner_dir/Libre-Closet/libre-closet.json.abs"
	printf '%s\n' 'stale repo mktemp' >"$owner_dir/Libre-Closet/libre-closet.json.ABC123"

	build_owner_arrays "$owner_dir"

	[ ! -f "$owner_dir/owner.json.tmp" ] || fail "Expected owner array creation to remove stale .json.tmp files"
	[ ! -f "$owner_dir/Libre-Closet/libre-closet.json.abs" ] || fail "Expected owner array creation to remove stale .json.abs files"
	[ ! -f "$owner_dir/Libre-Closet/libre-closet.json.ABC123" ] || fail "Expected owner array creation to remove stale .json.XXXXXX files"
}

test_owner_arrays_stream_json_into_jq() {
	local owner_dir="$workdir/stream/Lazztech"
	local empty_payload="$workdir/empty-stream.txt"

	: >"$empty_payload"
	mkdir -p "$owner_dir/Libre-Closet"

	write_package_json "$owner_dir/Libre-Closet/libre-closet.json" "Lazztech" "Libre-Closet" "libre-closet" 1 "$empty_payload"
	write_package_json "$owner_dir/Libre-Closet/libre-closet-dev.json" "Lazztech" "Libre-Closet" "libre-closet-dev" 1 "$empty_payload"

	jq() {
		local arg
		if [ "${1:-}" = "-cs" ] && [ "${2:-}" = "." ]; then
			for arg in "$@"; do
				[[ "$arg" == *.json ]] && fail "Expected owner array creation to stream JSON into jq instead of passing file paths"
			done
		fi

		command jq "$@"
	}

	build_owner_arrays "$owner_dir"
	unset -f jq

	assert_file_exists "$owner_dir/.json"
	assert_json_length "$owner_dir/.json" 2
}

test_large_array_trimming() {
	local payload_file="$workdir/payload.txt"
	local empty_payload="$workdir/empty-large.txt"
	local base_dir="$workdir/large"
	local owner_array="$base_dir/owner-large.json"
	local repo_array="$base_dir/repo-large.json"
	local owner_versions_before
	local owner_versions_after
	local repo_versions_before
	local repo_versions_after

	: >"$empty_payload"
	mkdir -p "$base_dir"
	head -c 700000 /dev/zero | tr '\0' 'a' >"$payload_file"

	write_package_json "$base_dir/libre-closet.json" "Lazztech" "Libre-Closet" "libre-closet" 75 "$payload_file"
	write_package_json "$base_dir/sidecar.json" "Lazztech" "SideRepo" "sidecar" 1 "$empty_payload"

	jq -cs '.' "$base_dir/libre-closet.json" "$base_dir/sidecar.json" >"$owner_array"
	jq -c '[.[] | select(.repo == "Libre-Closet")]' "$owner_array" >"$repo_array"

	assert_json_array "$owner_array"
	assert_json_array "$repo_array"
	assert_size_gt "$owner_array" 50000000
	assert_size_gt "$repo_array" 50000000

	owner_versions_before=$(version_total "$owner_array")
	repo_versions_before=$(version_total "$repo_array")

	bash "$src_dir/lib/ytoxt.sh" "$owner_array" >/dev/null
	bash "$src_dir/lib/ytoxt.sh" "$repo_array" >/dev/null

	assert_file_exists "${owner_array%.*}.xml"
	assert_file_exists "${repo_array%.*}.xml"
	assert_json_array "$owner_array"
	assert_json_array "$repo_array"
	assert_size_lt "$owner_array" 50000000
	assert_size_lt "${owner_array%.*}.xml" 50000000
	assert_size_lt "$repo_array" 50000000
	assert_size_lt "${repo_array%.*}.xml" 50000000
	assert_contains "${owner_array%.*}.xml" "libre-closet"
	assert_contains "${repo_array%.*}.xml" "libre-closet"
	assert_repo_only "$repo_array" "Libre-Closet"

	owner_versions_after=$(version_total "$owner_array")
	repo_versions_after=$(version_total "$repo_array")
	[ "$owner_versions_after" -lt "$owner_versions_before" ] || fail "Expected owner array trimming to remove versions"
	[ "$repo_versions_after" -lt "$repo_versions_before" ] || fail "Expected repo array trimming to remove versions"
}

trap cleanup EXIT

test_small_owner_and_repo_arrays
test_owner_arrays_cleanup_legacy_version_dirs
test_owner_arrays_cleanup_stale_json_sidecars
test_owner_arrays_stream_json_into_jq
test_large_array_trimming

echo "Array creation regression tests passed"