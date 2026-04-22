#!/bin/bash

# shellcheck disable=SC1091,SC2034

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
src_dir=${src_dir:?}
workdir=${workdir:?}

init_bkg_state() {
	local env_file=$1
	init_bkg_runtime_state "$env_file"
}

test_update_version_batches_rows_until_flush() {
	local test_root="$workdir/version-batch"
	local db_file="$test_root/test.db"

	mkdir -p "$test_root"

	(
		cd "$test_root"
		ln -s "$src_dir/lib" lib
		export BKG_SKIP_DEP_VERIFY=1
		source "$src_dir/lib/version.sh"
		init_bkg_state "$test_root/env.env"

		BKG_INDEX_DB="$db_file"
		owner='Lazztech'
		repo='Libre-Closet'
		package='libre-closet'
		owner_type='orgs'
		package_type='container'
		lower_owner='lazztech'
		lower_package='libre-closet'
		table_version_name='versions_orgs_container_Lazztech_Libre-Closet_libre-closet'

		sqlite3 "$BKG_INDEX_DB" "create table if not exists '$table_version_name' (id text not null, name text not null, size integer not null, downloads integer not null, downloads_month integer not null, downloads_week integer not null, downloads_day integer not null, date text not null, tags text, primary key (id, date));"
		version_stage_reset

		curl() {
			cat <<'EOF'
<span>Total downloads</span><span>984</span><span>Last 30 days</span><span>984</span><span>Last week</span><span>454</span><span>Today</span><span>2</span><pre><code>{"schemaVersion":2,"layers":[{"size":123}]}</code></pre>
EOF
		}

		docker_manifest_inspect() {
			printf '%s' '{"schemaVersion":2,"layers":[{"size":123}]}'
		}

		row_a=$(printf '%s' '{"id":101,"name":"sha256:a","tags":"latest"}' | base64 -w0)
		row_b=$(printf '%s' '{"id":102,"name":"sha256:b","tags":"stable"}' | base64 -w0)

		update_version "$row_a" >/dev/null
		update_version "$row_b" >/dev/null

		[ "$(find "$VERSION_STAGE_DIR" -maxdepth 1 -type f -name '*.sql' | wc -l)" -eq 2 ] || fail "Expected two staged version rows before flush"
		[ "$(sqlite3 "$BKG_INDEX_DB" "select count(*) from '$table_version_name';")" = "0" ] || fail "Expected no persisted version rows before batch flush"

		version_flush_staged_rows

		[ "$(sqlite3 "$BKG_INDEX_DB" "select count(*) from '$table_version_name';")" = "2" ] || fail "Expected two persisted version rows after batch flush"
		rows=$(sqlite3 "$BKG_INDEX_DB" "select id || '|' || downloads || '|' || downloads_month || '|' || downloads_week || '|' || downloads_day from '$table_version_name' order by id;")
		grep -Fxq '101|984|984|454|2' <<<"$rows" || fail "Expected flushed batch row for version 101"
		grep -Fxq '102|984|984|454|2' <<<"$rows" || fail "Expected flushed batch row for version 102"
	)
}

test_update_package_builds_version_array_from_db() {
	local test_root="$workdir/package-refresh"
	local db_file="$test_root/test.db"
	local today

	mkdir -p "$test_root"
	today=$(date -u +%Y-%m-%d)

	(
		cd "$test_root"
		ln -s "$src_dir/lib" lib
		export BKG_SKIP_DEP_VERIFY=1
		source "$src_dir/lib/package.sh"
		init_bkg_state "$test_root/env.env"

		BKG_INDEX_DB="$db_file"
		BKG_INDEX_DIR="$test_root/index"
		BKG_OPTOUT="$test_root/optout.txt"
		: >"$BKG_OPTOUT"
		BKG_OWNERS="$test_root/owners.txt"
		: >"$BKG_OWNERS"
		BKG_BATCH_FIRST_STARTED="$today"
		owner_id=69664378
		owner='Lazztech'
		repo='Libre-Closet'
		package='libre-closet'
		owner_type='orgs'
		package_type='container'
		fast_out=false
		BKG_MODE=0
		table_version_name='versions_orgs_container_Lazztech_Libre-Closet_libre-closet'
		mkdir -p "$BKG_INDEX_DIR/$owner/$repo"
		printf '%s\n' 'stale abs' >"$BKG_INDEX_DIR/$owner/$repo/$package.json.abs"
		printf '%s\n' 'stale tmp' >"$BKG_INDEX_DIR/$owner/$repo/$package.json.tmp"
		yesterday=$(date -u -d '1 day ago' +%Y-%m-%d)

		sqlite3 "$BKG_INDEX_DB" "create table if not exists '$BKG_INDEX_TBL_PKG' (owner_id text, owner_type text not null, package_type text not null, owner text not null, repo text not null, package text not null, downloads integer not null, downloads_month integer not null, downloads_week integer not null, downloads_day integer not null, size integer not null, date text not null, primary key (owner_id, package, date));"
		sqlite3 "$BKG_INDEX_DB" "create table if not exists '$table_version_name' (id text not null, name text not null, size integer not null, downloads integer not null, downloads_month integer not null, downloads_week integer not null, downloads_day integer not null, date text not null, tags text, primary key (id, date));"
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('69664378','orgs','container','Lazztech','Libre-Closet','libre-closet','2000','300','200','20','400','$today');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('69664378','orgs','container','Lazztech','Libre-Closet','repo-lower','1500','250','150','15','350','$today');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('69664378','orgs','container','Lazztech','AnotherRepo','owner-higher','2500','350','250','25','450','$today');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('69664378','orgs','container','Lazztech','Libre-Closet','stale-higher','9000','900','900','90','900','$yesterday');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$table_version_name' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('10','sha256:b','456','985','985','455','3','$today','latest');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$table_version_name' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('2','sha256:a','123','984','984','454','2','$today','stable');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$table_version_name' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('50','sha256:stale','999','9999','9999','9999','99','$yesterday','latest,stable');"
		printf '69664378|Lazztech|Libre-Closet|libre-closet|%s\n' "$today" >packages_already_updated

		update_package 'container/Libre-Closet/libre-closet' >/dev/null

		assert_file_exists "$BKG_INDEX_DIR/$owner/$repo/$package.json"
		[ ! -f "$BKG_INDEX_DIR/$owner/$repo/$package.json.abs" ] || fail "Expected package refresh to remove stale .json.abs files"
		[ ! -f "$BKG_INDEX_DIR/$owner/$repo/$package.json.tmp" ] || fail "Expected package refresh to remove stale .json.tmp files"
		jq -e '.raw_versions == 2 and .raw_owner_rank == 2 and .raw_repo_rank == 1 and (.version | map(.id) == [2,10]) and any(.version[]; .id == 10 and .latest == true and .newest == true) and any(.version[]; .id == 2 and (.tags | index("stable")))' "$BKG_INDEX_DIR/$owner/$repo/$package.json" >/dev/null || fail "Expected package JSON to embed numerically ordered version rows and preserve owner/repo rank semantics"
	)
}

test_update_package_handles_large_version_arrays() {
	local test_root="$workdir/package-refresh-large"
	local db_file="$test_root/test.db"
	local today
	local huge_name_file="$test_root/huge-name.txt"
	local sql_file="$test_root/insert-large.sql"

	mkdir -p "$test_root"
	today=$(date -u +%Y-%m-%d)
	head -c 2500000 /dev/zero | tr '\0' 'a' >"$huge_name_file"

	(
		cd "$test_root"
		ln -s "$src_dir/lib" lib
		export BKG_SKIP_DEP_VERIFY=1
		source "$src_dir/lib/package.sh"
		init_bkg_state "$test_root/env.env"

		BKG_INDEX_DB="$db_file"
		BKG_INDEX_DIR="$test_root/index"
		BKG_OPTOUT="$test_root/optout.txt"
		: >"$BKG_OPTOUT"
		BKG_OWNERS="$test_root/owners.txt"
		: >"$BKG_OWNERS"
		BKG_BATCH_FIRST_STARTED="$today"
		owner_id=69664378
		owner='Lazztech'
		repo='Libre-Closet'
		package='libre-closet'
		owner_type='orgs'
		package_type='container'
		fast_out=false
		BKG_MODE=0
		table_version_name='versions_orgs_container_Lazztech_Libre-Closet_libre-closet'
		mkdir -p "$BKG_INDEX_DIR/$owner/$repo"

		sqlite3 "$BKG_INDEX_DB" "create table if not exists '$BKG_INDEX_TBL_PKG' (owner_id text, owner_type text not null, package_type text not null, owner text not null, repo text not null, package text not null, downloads integer not null, downloads_month integer not null, downloads_week integer not null, downloads_day integer not null, size integer not null, date text not null, primary key (owner_id, package, date));"
		sqlite3 "$BKG_INDEX_DB" "create table if not exists '$table_version_name' (id text not null, name text not null, size integer not null, downloads integer not null, downloads_month integer not null, downloads_week integer not null, downloads_day integer not null, date text not null, tags text, primary key (id, date));"
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('69664378','orgs','container','Lazztech','Libre-Closet','libre-closet','2000','300','200','20','400','$today');"
		{
			printf "insert into '%s' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('101','" "$table_version_name"
			cat "$huge_name_file"
			printf "','123','984','984','454','2','%s','latest');\n" "$today"
		} >"$sql_file"
		sqlite3 "$BKG_INDEX_DB" <"$sql_file"
		printf '69664378|Lazztech|Libre-Closet|libre-closet|%s\n' "$today" >packages_already_updated

		update_package 'container/Libre-Closet/libre-closet' >/dev/null

		assert_file_exists "$BKG_INDEX_DIR/$owner/$repo/$package.json"
		jq -e --argjson expected 2500000 '.raw_versions == 1 and (.version | length) == 1 and (.version[0].name | length) == $expected' "$BKG_INDEX_DIR/$owner/$repo/$package.json" >/dev/null || fail "Expected package refresh to handle very large version arrays without hitting ARG_MAX"
	)
}

test_update_package_uses_persisted_batch_start_when_shell_var_missing() {
	local test_root="$workdir/package-refresh-batch-state"
	local db_file="$test_root/test.db"
	local today
	local yesterday

	mkdir -p "$test_root"
	today=$(date -u +%Y-%m-%d)
	yesterday=$(date -u -d '1 day ago' +%Y-%m-%d)

	(
		cd "$test_root"
		ln -s "$src_dir/lib" lib
		export BKG_SKIP_DEP_VERIFY=1
		source "$src_dir/lib/package.sh"
		init_bkg_state "$test_root/env.env"

		BKG_INDEX_DB="$db_file"
		BKG_INDEX_DIR="$test_root/index"
		BKG_OPTOUT="$test_root/optout.txt"
		: >"$BKG_OPTOUT"
		BKG_OWNERS="$test_root/owners.txt"
		: >"$BKG_OWNERS"
		set_BKG BKG_BATCH_FIRST_STARTED "$today"
		unset BKG_BATCH_FIRST_STARTED
		owner_id=69664378
		owner='Lazztech'
		repo='Libre-Closet'
		package='libre-closet'
		owner_type='orgs'
		package_type='container'
		fast_out=false
		BKG_MODE=0
		table_version_name='versions_orgs_container_Lazztech_Libre-Closet_libre-closet'
		mkdir -p "$BKG_INDEX_DIR/$owner/$repo"

		sqlite3 "$BKG_INDEX_DB" "create table if not exists '$BKG_INDEX_TBL_PKG' (owner_id text, owner_type text not null, package_type text not null, owner text not null, repo text not null, package text not null, downloads integer not null, downloads_month integer not null, downloads_week integer not null, downloads_day integer not null, size integer not null, date text not null, primary key (owner_id, package, date));"
		sqlite3 "$BKG_INDEX_DB" "create table if not exists '$table_version_name' (id text not null, name text not null, size integer not null, downloads integer not null, downloads_month integer not null, downloads_week integer not null, downloads_day integer not null, date text not null, tags text, primary key (id, date));"
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('69664378','orgs','container','Lazztech','Libre-Closet','libre-closet','2000','300','200','20','400','$today');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$table_version_name' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('10','sha256:current','456','985','985','455','3','$today','latest');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$table_version_name' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('2','sha256:stale','123','984','984','454','2','$yesterday','stable');"
		printf '69664378|Lazztech|Libre-Closet|libre-closet|%s\n' "$today" >packages_already_updated

		update_package 'container/Libre-Closet/libre-closet' >/dev/null

		assert_file_exists "$BKG_INDEX_DIR/$owner/$repo/$package.json"
		jq -e '.raw_versions == 1 and (.version | map(.id) == [10])' "$BKG_INDEX_DIR/$owner/$repo/$package.json" >/dev/null || fail "Expected update_package to honor persisted BKG_BATCH_FIRST_STARTED when the shell variable is unset"
	)
}

test_version_parse_page_html_escapes_control_characters() {
	local html
	local parsed

	(
		cd "$workdir"
		ln -s "$src_dir/lib" lib
		export BKG_SKIP_DEP_VERIFY=1
		source "$src_dir/lib/version.sh"
		owner='Lazztech'
		repo='Libre-Closet'
		package='libre-closet'
		owner_type='orgs'
		package_type='container'

		html=$(cat <<'EOF'
<li class="Box-row">
	<a href="/Lazztech/Libre-Closet/pkgs/container/libre-closet/123?tag=release%09tag"></a>
  <input value="sha256:line%09name%0Dtest" />
</li>
EOF
)
		parsed=$(version_parse_page_html "$html")
		jq -e '.[0].id == 123 and .[0].name == "sha256:line\tname\rtest" and .[0].tags == ["release\ttag"]' <<<"$parsed" >/dev/null || fail "Expected version_parse_page_html to escape control characters before jq parsing"
	)
}

run_update_package_append_tagged_versions_scenario() {
	local test_root=$1
	local append_limit=$2
	local tag_cache_pages=$3
	local expected_total=$4
	local expected_first_id=$5
	local expected_missing_below_id=$6
	local db_file="$test_root/test.db"
	local today

	mkdir -p "$test_root"
	today=$(date -u +%Y-%m-%d)

	(
		cd "$test_root"
		ln -s "$src_dir/lib" lib
		export BKG_SKIP_DEP_VERIFY=1
		source "$src_dir/lib/package.sh"
		init_bkg_state "$test_root/env.env"

		BKG_INDEX_DB="$db_file"
		BKG_INDEX_DIR="$test_root/index"
		BKG_OPTOUT="$test_root/optout.txt"
		: >"$BKG_OPTOUT"
		BKG_OWNERS="$test_root/owners.txt"
		: >"$BKG_OWNERS"
		BKG_BATCH_FIRST_STARTED="$today"
		BKG_APPEND_TAGGED_VERSIONS_LIMIT="$append_limit"
		BKG_TAG_CACHE_PAGES="$tag_cache_pages"
		owner_id=69664378
		owner='Lazztech'
		repo='Libre-Closet'
		package='libre-closet'
		owner_type='orgs'
		package_type='container'
		fast_out=false
		BKG_MODE=0
		table_version_name='versions_orgs_container_Lazztech_Libre-Closet_libre-closet'
		mkdir -p "$BKG_INDEX_DIR/$owner/$repo"
		: >packages_already_updated

		sqlite3 "$BKG_INDEX_DB" "create table if not exists '$BKG_INDEX_TBL_PKG' (owner_id text, owner_type text not null, package_type text not null, owner text not null, repo text not null, package text not null, downloads integer not null, downloads_month integer not null, downloads_week integer not null, downloads_day integer not null, size integer not null, date text not null, primary key (owner_id, package, date));"
		sqlite3 "$BKG_INDEX_DB" "create table if not exists '$table_version_name' (id text not null, name text not null, size integer not null, downloads integer not null, downloads_month integer not null, downloads_week integer not null, downloads_day integer not null, date text not null, tags text, primary key (id, date));"
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('69664378','orgs','container','Lazztech','Libre-Closet','libre-closet','2000','300','200','20','400','$today');"

		curl() {
			printf '%s' 'Total downloads"2000'
		}

		page_version() {
			[ -n "$1" ] || return 1
			VERSION_PAGE_JSON=$(jq -cn '[range(130;100;-1) | {id: ., name: ("sha256:" + (. | tostring))}]')
			VERSION_PAGE_COUNT=30
			return 2
		}

		version_load_tag_cache_page() {
			[ -n "$1" ] || return 1
			local version_id
			local candidate_line
			local -a version_ids=()

			case "$1" in
			1)
				mapfile -t version_ids < <(seq 100 -1 71)
				;;
			2)
				mapfile -t version_ids < <(seq 70 -1 66)
				VERSION_TAG_CACHE_EXHAUSTED=true
				;;
			*)
				VERSION_TAG_CACHE_EXHAUSTED=true
				version_ids=()
				;;
			esac

			for version_id in "${version_ids[@]}"; do
				candidate_line=$(jq -cn --argjson id "$version_id" --arg name "sha256:$version_id" --arg tag "tag-$version_id" '{id: $id, name: $name, tags: [$tag]}' | base64 -w0)
				VERSION_SOURCE_LINES["$version_id"]="$candidate_line"
				VERSION_TAG_CACHE["$version_id"]="tag-$version_id"

				if [ -z "${VERSION_TAGGED_IDS_SEEN[$version_id]+x}" ]; then
					VERSION_TAGGED_IDS+=("$version_id")
					VERSION_TAGGED_IDS_SEEN["$version_id"]=1
				fi
			done

			VERSION_TAG_CACHE_PAGES_FETCHED=$1
			((${#version_ids[@]} >= 30)) || VERSION_TAG_CACHE_EXHAUSTED=true
		}

		update_version() {
			local version_id
			local version_name
			local version_tags

			version_id=$(_jq "$1" '.id')
			version_name=$(_jq "$1" '.name')
			version_tags=$(_jq "$1" '.tags')
			sqlite3 "$BKG_INDEX_DB" "insert or replace into '$table_version_name' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('$version_id', '$version_name', 123, 10, 10, 10, 10, '$today', '$version_tags');"
		}

		update_package 'container/Libre-Closet/libre-closet' >/dev/null

		assert_file_exists "$BKG_INDEX_DIR/$owner/$repo/$package.json"
		jq -e \
			--argjson expected_total "$expected_total" \
			--argjson expected_first_id "$expected_first_id" \
			--argjson expected_missing_below_id "$expected_missing_below_id" \
			'.raw_versions == $expected_total and (.version | length) == $expected_total and .version[0].id == $expected_first_id and .version[-1].id == 130 and any(.version[]; .id == 100 and (.tags | index("tag-100"))) and all(.version[]; .id > $expected_missing_below_id)' \
			"$BKG_INDEX_DIR/$owner/$repo/$package.json" >/dev/null || fail "Expected update_package to honor the configured append limit for older tagged versions"
	)
}

test_update_package_appends_older_tagged_versions_beyond_output_window() {
	run_update_package_append_tagged_versions_scenario "$workdir/package-refresh-append-tagged" 30 3 60 71 70
}

test_update_package_honors_configured_append_tagged_limit() {
	run_update_package_append_tagged_versions_scenario "$workdir/package-refresh-append-tagged-limited" 7 3 37 94 93
}

test_update_package_honors_configured_tag_cache_pages_for_appended_versions() {
	run_update_package_append_tagged_versions_scenario "$workdir/package-refresh-append-tagged-one-tag-page" 35 1 60 71 70
}

run_update_package_window_tagged_promotion_scenario() {
	local test_root=$1
	local max_version_pages=$2
	local tag_cache_pages=$3
	local expected_first_id=$4
	local expected_has_hundred=$5
	local db_file="$test_root/test.db"
	local today

	mkdir -p "$test_root"
	today=$(date -u +%Y-%m-%d)

	(
		cd "$test_root"
		ln -s "$src_dir/lib" lib
		export BKG_SKIP_DEP_VERIFY=1
		source "$src_dir/lib/package.sh"
		init_bkg_state "$test_root/env.env"

		BKG_INDEX_DB="$db_file"
		BKG_INDEX_DIR="$test_root/index"
		BKG_OPTOUT="$test_root/optout.txt"
		: >"$BKG_OPTOUT"
		BKG_OWNERS="$test_root/owners.txt"
		: >"$BKG_OWNERS"
		BKG_BATCH_FIRST_STARTED="$today"
		BKG_APPEND_TAGGED_VERSIONS_LIMIT=0
		BKG_MAX_VERSION_PAGES="$max_version_pages"
		BKG_TAG_CACHE_PAGES="$tag_cache_pages"
		owner_id=69664378
		owner='Lazztech'
		repo='Libre-Closet'
		package='libre-closet'
		owner_type='orgs'
		package_type='container'
		fast_out=false
		BKG_MODE=0
		table_version_name='versions_orgs_container_Lazztech_Libre-Closet_libre-closet'
		mkdir -p "$BKG_INDEX_DIR/$owner/$repo"
		: >packages_already_updated

		sqlite3 "$BKG_INDEX_DB" "create table if not exists '$BKG_INDEX_TBL_PKG' (owner_id text, owner_type text not null, package_type text not null, owner text not null, repo text not null, package text not null, downloads integer not null, downloads_month integer not null, downloads_week integer not null, downloads_day integer not null, size integer not null, date text not null, primary key (owner_id, package, date));"
		sqlite3 "$BKG_INDEX_DB" "create table if not exists '$table_version_name' (id text not null, name text not null, size integer not null, downloads integer not null, downloads_month integer not null, downloads_week integer not null, downloads_day integer not null, date text not null, tags text, primary key (id, date));"
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('69664378','orgs','container','Lazztech','Libre-Closet','libre-closet','2000','300','200','20','400','$today');"

		curl() {
			printf '%s' 'Total downloads"2000'
		}

		page_version() {
			case "$1" in
			1)
				VERSION_PAGE_JSON=$(jq -cn '[range(130;100;-1) | {id: ., name: ("sha256:" + (. | tostring))}]')
				VERSION_PAGE_COUNT=30
				return 0
				;;
			2)
				VERSION_PAGE_JSON=$(jq -cn '[range(100;70;-1) | {id: ., name: ("sha256:" + (. | tostring))}]')
				VERSION_PAGE_COUNT=30
				return 2
				;;
			esac

			VERSION_PAGE_JSON='[]'
			VERSION_PAGE_COUNT=0
			return 2
		}

		version_load_tag_cache_page() {
			[ "$1" = "1" ] || {
				VERSION_TAG_CACHE_EXHAUSTED=true
				return 0
			}

			local version_id
			local candidate_line

			for version_id in $(seq 100 -1 91); do
				candidate_line=$(jq -cn --argjson id "$version_id" --arg name "sha256:$version_id" --arg tag "tag-$version_id" '{id: $id, name: $name, tags: [$tag]}' | base64 -w0)
				VERSION_SOURCE_LINES["$version_id"]="$candidate_line"
				VERSION_TAG_CACHE["$version_id"]="tag-$version_id"

				if [ -z "${VERSION_TAGGED_IDS_SEEN[$version_id]+x}" ]; then
					VERSION_TAGGED_IDS+=("$version_id")
					VERSION_TAGGED_IDS_SEEN["$version_id"]=1
				fi
			done

			VERSION_TAG_CACHE_PAGES_FETCHED=1
			VERSION_TAG_CACHE_EXHAUSTED=true
		}

		update_version() {
			local version_id
			local version_name
			local version_tags

			version_id=$(_jq "$1" '.id')
			version_name=$(_jq "$1" '.name')
			version_tags=$(_jq "$1" '.tags')
			sqlite3 "$BKG_INDEX_DB" "insert or replace into '$table_version_name' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('$version_id', '$version_name', 123, 10, 10, 10, 10, '$today', '$version_tags');"
		}

		update_package 'container/Libre-Closet/libre-closet' >/dev/null

		assert_file_exists "$BKG_INDEX_DIR/$owner/$repo/$package.json"
		jq -e \
			--argjson expected_first_id "$expected_first_id" \
			--arg expected_has_hundred "$expected_has_hundred" \
			'.raw_versions == 30 and (.version | length) == 30 and .version[0].id == $expected_first_id and .version[-1].id == 130 and (($expected_has_hundred == "true") == any(.version[]; .id == 100 and (.tags | index("tag-100"))))' \
			"$BKG_INDEX_DIR/$owner/$repo/$package.json" >/dev/null || fail "Expected update_package to honor the configured page limits for tagged promotion"
	)
}

test_update_package_honors_configured_max_version_pages() {
	run_update_package_window_tagged_promotion_scenario "$workdir/package-refresh-max-pages" 1 1 101 false
}

test_update_package_honors_configured_tag_cache_pages() {
	run_update_package_window_tagged_promotion_scenario "$workdir/package-refresh-zero-tag-pages" 2 0 101 false
}

trap cleanup EXIT

run_test test_update_version_batches_rows_until_flush
run_test test_update_package_builds_version_array_from_db
run_test test_update_package_handles_large_version_arrays
run_test test_update_package_uses_persisted_batch_start_when_shell_var_missing
run_test test_version_parse_page_html_escapes_control_characters
run_test test_update_package_appends_older_tagged_versions_beyond_output_window
run_test test_update_package_honors_configured_append_tagged_limit
run_test test_update_package_honors_configured_tag_cache_pages_for_appended_versions
run_test test_update_package_honors_configured_max_version_pages
run_test test_update_package_honors_configured_tag_cache_pages

echo "Version DB regression tests passed"