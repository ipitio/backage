#!/bin/bash

# Test configuration is consumed through Bash dynamic scope.
# shellcheck disable=SC1091,SC2034

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
	mapfile -d '' -t json_files < <(find "$owner_dir" -type f -name '*.json' ! -name '.*' -print0 | LC_ALL=C sort -z)
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

test_owner_arrays_cleanup_stale_json_sidecars() {
	local owner_dir="$workdir/sidecars/Lazztech"
	local empty_payload="$workdir/empty-sidecars.txt"

	: >"$empty_payload"
	mkdir -p "$owner_dir/Libre-Closet"

	write_package_json "$owner_dir/Libre-Closet/libre-closet.json" "Lazztech" "Libre-Closet" "libre-closet" 1 "$empty_payload"
	printf '%s\n' 'stale owner tmp' >"$owner_dir/owner.json.tmp"
	printf '%s\n' 'stale repo abs' >"$owner_dir/Libre-Closet/libre-closet.json.abs"

	build_owner_arrays "$owner_dir"

	[ ! -f "$owner_dir/owner.json.tmp" ] || fail "Expected owner array creation to remove stale .json.tmp files"
	[ ! -f "$owner_dir/Libre-Closet/libre-closet.json.abs" ] || fail "Expected owner array creation to remove stale .json.abs files"
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

test_owner_build_json_array_limits_versions_before_aggregate() {
	local owner_dir="$workdir/project-array-limit/Lazztech/Libre-Closet"
	local output_file="$workdir/project-array-limit.json"

	mkdir -p "$owner_dir"
	jq -nc '
		{
			owner: "Lazztech",
			repo: "Libre-Closet",
			package: "libre-closet",
			raw_downloads: 1,
			version: [
				{id: 1, latest: true},
				{id: 2},
				{id: 3},
				{id: 4},
				{id: 5, newest: true}
			]
		}' >"$owner_dir/libre-closet.json"

	source_project_script "lib/owner.sh"
	init_bkg_runtime_state "$workdir/env-project-array-limit.env"
	BKG_OWNER_ARRAY_VERSION_LIMIT=2
	owner_build_json_array "$workdir/project-array-limit/Lazztech" >"$output_file"

	assert_json_array "$output_file"
	jq -e '.[0].version | map(.id) == [1,4,5]' "$output_file" >/dev/null || fail "Expected owner aggregate generation to keep latest/newest and a bounded recent version slice"
	unset BKG_OWNER_ARRAY_VERSION_LIMIT
}

test_owner_build_json_array_adapts_to_byte_budget() {
	local owner_dir="$workdir/project-array-budget/Lazztech/Libre-Closet"
	local fixed_two_file="$workdir/project-array-budget-two.json"
	local fixed_three_file="$workdir/project-array-budget-three.json"
	local adaptive_file="$workdir/project-array-budget-adaptive.json"
	local payload_file="$workdir/project-array-budget-payload.txt"
	local target_size

	mkdir -p "$owner_dir"
	head -c 4000 /dev/zero | tr '\0' 'a' >"$payload_file"
	jq -nc --rawfile payload "$payload_file" '
		{
			owner: "Lazztech",
			repo: "Libre-Closet",
			package: "libre-closet",
			raw_downloads: 1,
			version: [
				{id: 1, latest: true, notes: $payload},
				{id: 2, notes: $payload},
				{id: 3, notes: $payload},
				{id: 4, notes: $payload},
				{id: 5, newest: true, notes: $payload}
			]
		}' >"$owner_dir/libre-closet.json"

	source_project_script "lib/owner.sh"
	init_bkg_runtime_state "$workdir/env-project-array-budget.env"
	BKG_OWNER_ARRAY_VERSION_LIMIT=2
	owner_build_json_array "$workdir/project-array-budget/Lazztech" >"$fixed_two_file"
	BKG_OWNER_ARRAY_VERSION_LIMIT=3
	owner_build_json_array "$workdir/project-array-budget/Lazztech" >"$fixed_three_file"
	target_size=$(stat -c %s "$fixed_two_file")

	unset BKG_OWNER_ARRAY_VERSION_LIMIT
	BKG_OWNER_ARRAY_MAX_BYTES="$target_size"
	owner_build_json_array "$workdir/project-array-budget/Lazztech" >"$adaptive_file"

	assert_json_array "$adaptive_file"
	assert_size_lt "$adaptive_file" "$((target_size + 1))"
	jq -e '.[0].version | map(.id) == [1,4,5]' "$adaptive_file" >/dev/null || fail "Expected adaptive owner aggregate generation to choose the largest version slice within the byte budget"
	[ "$(stat -c %s "$fixed_three_file")" -gt "$target_size" ] || fail "Expected fixed limit 3 fixture to exceed the adaptive byte budget"
	unset BKG_OWNER_ARRAY_MAX_BYTES
}

test_owner_build_json_array_from_db_ignores_stale_package_json() {
	local test_root="$workdir/project-array-db"
	local db_file="$test_root/test.db"
	local owner_dir="$test_root/index/Lazztech"
	local output_file="$test_root/owner.json"
	local repo_output_file="$test_root/repo.json"
	local today="2026-03-30"

	mkdir -p "$owner_dir/Libre-Closet" "$owner_dir/SideRepo"
	jq -nc '{owner: "Stale", repo: "Libre-Closet", package: "stale-json", version: [{id: 999}]}' >"$owner_dir/Libre-Closet/stale.json"

	(
		source_project_script "lib/owner.sh"
		init_bkg_runtime_state "$test_root/env.env"
		BKG_INDEX_DB="$db_file"
		BKG_INDEX_DIR="$test_root/index"
		BKG_BATCH_FIRST_STARTED="$today"
		BKG_OWNER_ARRAY_VERSION_LIMIT=-1
		set_BKG BKG_BATCH_FIRST_STARTED "$today"

		sqlite_ensure_index_schema >/dev/null
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('69664378','orgs','container','Lazztech','Libre-Closet','libre-closet','2000','300','200','20','400','$today');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('69664378','orgs','container','Lazztech','SideRepo','sidecar','3000','350','250','25','450','$today');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_VER' (owner_id, owner_type, package_type, owner, repo, package, id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('69664378','orgs','container','Lazztech','Libre-Closet','libre-closet','10','sha256:b','456','985','985','455','3','$today','latest');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_VER' (owner_id, owner_type, package_type, owner, repo, package, id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('69664378','orgs','container','Lazztech','Libre-Closet','libre-closet','2','sha256:a','123','984','984','454','2','$today','stable');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_VER' (owner_id, owner_type, package_type, owner, repo, package, id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('69664378','orgs','container','Lazztech','SideRepo','sidecar','5','sha256:c','222','111','22','11','1','$today','latest');"

		owner_build_json_array_from_db_to_file "69664378" "" "$owner_dir" "$output_file"
		owner_build_json_array_from_db_to_file "69664378" "Libre-Closet" "$owner_dir/Libre-Closet" "$repo_output_file"
	)

	assert_json_array "$output_file"
	assert_json_length "$output_file" 2
	jq -e 'map(.package) | sort == ["libre-closet","sidecar"]' "$output_file" >/dev/null || fail "Expected DB-backed owner aggregate to render packages from SQLite"
	jq -e 'all(.[]; .package != "stale-json")' "$output_file" >/dev/null || fail "Expected DB-backed owner aggregate to ignore stale package JSON files"
	jq -e '.[] | select(.package == "libre-closet") | .version | map(.id) == [2,10]' "$output_file" >/dev/null || fail "Expected DB-backed owner aggregate to render normalized version rows"
	jq -e '.[] | select(.package == "libre-closet") | .raw_owner_rank == 2 and .raw_repo_rank == 1' "$output_file" >/dev/null || fail "Expected DB-backed owner aggregate to use precomputed owner/repo ranks"

	assert_json_array "$repo_output_file"
	assert_json_length "$repo_output_file" 1
	assert_repo_only "$repo_output_file" "Libre-Closet"
	jq -e '.[0].package == "libre-closet" and (.[0].version | map(.id) == [2,10])' "$repo_output_file" >/dev/null || fail "Expected DB-backed repo aggregate to render only the requested repo"
}

test_owner_build_json_array_from_db_adapts_large_hints_from_estimate() {
	local test_root="$workdir/project-array-db-bounded"
	local db_file="$test_root/test.db"
	local owner_dir="$test_root/index/Lazztech"
	local tight_output_file="$test_root/owner-tight.json"
	local roomy_output_file="$test_root/owner-roomy.json"
	local today="2026-03-30"

	mkdir -p "$owner_dir/Libre-Closet"
	head -c 20000 /dev/zero | tr '\0' 'a' >"$owner_dir/Libre-Closet/stale.json"

	(
		source_project_script "lib/owner.sh"
		init_bkg_runtime_state "$test_root/env.env"
		BKG_INDEX_DB="$db_file"
		BKG_INDEX_DIR="$test_root/index"
		BKG_BATCH_FIRST_STARTED="$today"
		unset BKG_OWNER_ARRAY_VERSION_LIMIT
		unset BKG_OWNER_ARRAY_DB_VERSION_LIMIT
		set_BKG BKG_BATCH_FIRST_STARTED "$today"

		sqlite_ensure_index_schema >/dev/null
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('69664378','orgs','container','Lazztech','Libre-Closet','libre-closet','2000','300','200','20','400','$today');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_VER' (owner_id, owner_type, package_type, owner, repo, package, id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('69664378','orgs','container','Lazztech','Libre-Closet','libre-closet','1','sha256:a','111','100','10','5','1','$today','latest');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_VER' (owner_id, owner_type, package_type, owner, repo, package, id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('69664378','orgs','container','Lazztech','Libre-Closet','libre-closet','2','sha256:b','222','200','20','10','2','$today','stable');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_VER' (owner_id, owner_type, package_type, owner, repo, package, id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('69664378','orgs','container','Lazztech','Libre-Closet','libre-closet','3','sha256:c','333','300','30','15','3','$today','');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_VER' (owner_id, owner_type, package_type, owner, repo, package, id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('69664378','orgs','container','Lazztech','Libre-Closet','libre-closet','4','sha256:d','444','400','40','20','4','$today','');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_VER' (owner_id, owner_type, package_type, owner, repo, package, id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('69664378','orgs','container','Lazztech','Libre-Closet','libre-closet','5','sha256:e','555','500','50','25','5','$today','');"

		BKG_OWNER_ARRAY_MAX_BYTES=10
		owner_build_json_array_from_db_to_file "69664378" "" "$owner_dir" "$tight_output_file"
		BKG_OWNER_ARRAY_MAX_BYTES=10000
		owner_build_json_array_from_db_to_file "69664378" "" "$owner_dir" "$roomy_output_file"
	)

	assert_json_array "$tight_output_file"
	assert_json_array "$roomy_output_file"
	jq -e '.[0].version | map(.id) == [1,5]' "$tight_output_file" >/dev/null || fail "Expected tight DB-backed aggregate budgets to keep latest/newest versions"
	jq -e '.[0].version | map(.id) == [1,2,3,4,5]' "$roomy_output_file" >/dev/null || fail "Expected roomy DB-backed aggregate budgets to adapt above the fallback slice"
	[ -z "$(find "$owner_dir" -type f \( -name '*.json.tmp.*' -o -name '*.json.abs.*' -o -name '*.json.rel.*' \) -print -quit)" ] || fail "Expected DB aggregate generation to avoid adaptive retry sidecars"
}

test_owner_build_json_array_from_db_falls_back_for_legacy_version_tables() {
	local test_root="$workdir/project-array-db-legacy-fallback"
	local db_file="$test_root/test.db"
	local owner_dir="$test_root/index/Lazztech"
	local output_file="$test_root/owner.json"
	local legacy_table="versions_orgs_container_Lazztech_LegacyRepo_legacy-only"
	local today="2026-03-30"

	mkdir -p "$owner_dir/LegacyRepo"
	head -c 20000 /dev/zero | tr '\0' 'a' >"$owner_dir/LegacyRepo/stale.json"

	(
		source_project_script "lib/owner.sh"
		init_bkg_runtime_state "$test_root/env.env"
		BKG_INDEX_DB="$db_file"
		BKG_INDEX_DIR="$test_root/index"
		BKG_BATCH_FIRST_STARTED="$today"
		unset BKG_OWNER_ARRAY_VERSION_LIMIT
		unset BKG_OWNER_ARRAY_DB_VERSION_LIMIT
		set_BKG BKG_BATCH_FIRST_STARTED "$today"

		sqlite_ensure_index_schema >/dev/null
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('69664378','orgs','container','Lazztech','LegacyRepo','legacy-only','2000','300','200','20','400','$today');"
		sqlite3 "$BKG_INDEX_DB" "create table if not exists '$legacy_table' (id text not null, name text not null, size integer not null, downloads integer not null, downloads_month integer not null, downloads_week integer not null, downloads_day integer not null, date text not null, tags text, primary key (id, date));"
		sqlite3 "$BKG_INDEX_DB" "insert into '$legacy_table' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('1','sha256:a','111','100','10','5','1','$today','latest');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$legacy_table' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('2','sha256:b','222','200','20','10','2','$today','stable');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$legacy_table' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('3','sha256:c','333','300','30','15','3','$today','');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$legacy_table' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('4','sha256:d','444','400','40','20','4','$today','');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$legacy_table' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('5','sha256:e','555','500','50','25','5','$today','');"

		BKG_OWNER_ARRAY_MAX_BYTES=10000
		owner_build_json_array_from_db_to_file "69664378" "" "$owner_dir" "$output_file"
	)

	assert_json_array "$output_file"
	jq -e '.[0].raw_versions == 5 and (.[0].version | map(.id) == [1,4,5])' "$output_file" >/dev/null || fail "Expected legacy-table DB aggregates to use the conservative fallback limit"
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

test_unsorted_version_arrays_still_trim_by_numeric_id() {
	local payload_file="$workdir/payload-unsorted.txt"
	local json_file="$workdir/unsorted-package.json"

	head -c 13000000 /dev/zero | tr '\0' 'a' >"$payload_file"

	jq -nc \
		--rawfile payload "$payload_file" \
		--arg date "2026-03-30" '
		{
			owner: "Lazztech",
			repo: "Libre-Closet",
			package: "libre-closet",
			downloads: "1",
			raw_downloads: 1,
			date: $date,
			version: [
				{id: 5, name: "v5", tags: ["latest"], downloads: "1", raw_downloads: 1, date: $date, notes: $payload},
				{id: 1, name: "v1", tags: ["tag-1"], downloads: "1", raw_downloads: 1, date: $date, notes: $payload},
				{id: 4, name: "v4", tags: ["tag-4"], downloads: "1", raw_downloads: 1, date: $date, notes: $payload},
				{id: 3, name: "v3", tags: ["tag-3"], downloads: "1", raw_downloads: 1, date: $date, notes: $payload}
			]
		}' >"$json_file"

	bash "$src_dir/lib/ytoxt.sh" "$json_file" >/dev/null

	jq -e '.version | map(.id) == [4,5]' "$json_file" >/dev/null || fail "Expected ytoxt.sh to keep trimming by numeric version id even when input version arrays are unsorted"
}

test_ytoxt_trimming_preserves_latest_and_newest_versions() {
	local payload_file="$workdir/payload-latest-newest.txt"
	local json_file="$workdir/latest-newest-package.json"

	head -c 2000 /dev/zero | tr '\0' 'a' >"$payload_file"

	jq -nc \
		--rawfile payload "$payload_file" \
		--arg date "2026-03-30" '
		{
			owner: "Lazztech",
			repo: "Libre-Closet",
			package: "libre-closet",
			downloads: "1",
			raw_downloads: 1,
			date: $date,
			version: [
				{id: 1, name: "v1", latest: true, downloads: "1", raw_downloads: 1, date: $date, notes: $payload},
				{id: 2, name: "v2", downloads: "1", raw_downloads: 1, date: $date, notes: $payload},
				{id: 3, name: "v3", downloads: "1", raw_downloads: 1, date: $date, notes: $payload},
				{id: 4, name: "v4", downloads: "1", raw_downloads: 1, date: $date, notes: $payload},
				{id: 5, name: "v5", downloads: "1", raw_downloads: 1, date: $date, notes: $payload},
				{id: 6, name: "v6", newest: true, downloads: "1", raw_downloads: 1, date: $date, notes: $payload}
			]
		}' >"$json_file"

	BKG_JSON_XML_MAX_BYTES=9000 BKG_JSON_XML_HARD_MAX_BYTES=200000 bash "$src_dir/lib/ytoxt.sh" "$json_file" >/dev/null

	jq -e 'any(.version[]; .id == 1 and .latest == true) and any(.version[]; .id == 6 and .newest == true)' "$json_file" >/dev/null || fail "Expected ytoxt.sh trimming to preserve latest and newest versions"
	assert_size_lt "$json_file" 9000
	assert_size_lt "${json_file%.*}.xml" 9000
}

trap cleanup EXIT

run_test test_small_owner_and_repo_arrays
run_test test_owner_arrays_cleanup_stale_json_sidecars
run_test test_owner_arrays_stream_json_into_jq
run_test test_owner_build_json_array_limits_versions_before_aggregate
run_test test_owner_build_json_array_adapts_to_byte_budget
run_test test_owner_build_json_array_from_db_ignores_stale_package_json
run_test test_owner_build_json_array_from_db_adapts_large_hints_from_estimate
run_test test_owner_build_json_array_from_db_falls_back_for_legacy_version_tables
run_test test_large_array_trimming
run_test test_unsorted_version_arrays_still_trim_by_numeric_id
run_test test_ytoxt_trimming_preserves_latest_and_newest_versions

echo "Array creation regression tests passed"
