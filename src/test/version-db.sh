#!/bin/bash

# shellcheck disable=SC1091,SC2034

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
src_dir=${src_dir:?}
workdir=${workdir:?}

init_bkg_state() {
	local env_file=$1
	local now

	BKG_ENV="$env_file"
	: >"$BKG_ENV"
	now=$(date -u +%s)
	set_BKG BKG_SCRIPT_START "$now"
	set_BKG BKG_RATE_LIMIT_START "$now"
	set_BKG BKG_MIN_RATE_LIMIT_START "$now"
	set_BKG BKG_CALLS_TO_API 0
	set_BKG BKG_MIN_CALLS_TO_API 0
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
		lower_owner='lazztech'
		lower_package='libre-closet'
		fast_out=false
		BKG_MODE=0
		table_version_name='versions_orgs_container_Lazztech_Libre-Closet_libre-closet'

		sqlite3 "$BKG_INDEX_DB" "create table if not exists '$BKG_INDEX_TBL_PKG' (owner_id text, owner_type text not null, package_type text not null, owner text not null, repo text not null, package text not null, downloads integer not null, downloads_month integer not null, downloads_week integer not null, downloads_day integer not null, size integer not null, date text not null, primary key (owner_id, package, date));"
		sqlite3 "$BKG_INDEX_DB" "create table if not exists '$table_version_name' (id text not null, name text not null, size integer not null, downloads integer not null, downloads_month integer not null, downloads_week integer not null, downloads_day integer not null, date text not null, tags text, primary key (id, date));"
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('69664378','orgs','container','Lazztech','Libre-Closet','libre-closet','2000','300','200','20','400','$today');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$table_version_name' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('101','sha256:a','123','984','984','454','2','$today','stable');"
		sqlite3 "$BKG_INDEX_DB" "insert into '$table_version_name' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('102','sha256:b','456','985','985','455','3','$today','latest');"
		printf '69664378|Lazztech|Libre-Closet|libre-closet|%s\n' "$today" >packages_already_updated

		save_version() {
			fail "save_version should not be called during package refresh"
		}

		update_package 'container/Libre-Closet/libre-closet' >/dev/null

		assert_file_exists "$BKG_INDEX_DIR/$owner/$repo/$package.json"
		jq -e '.raw_versions == 2 and (.version | length) == 2 and any(.version[]; .id == 102 and .latest == true and .newest == true) and any(.version[]; .id == 101 and (.tags | index("stable")))' "$BKG_INDEX_DIR/$owner/$repo/$package.json" >/dev/null || fail "Expected package JSON to embed version rows directly from the database"
	)
}

trap cleanup EXIT

test_update_version_batches_rows_until_flush
test_update_package_builds_version_array_from_db

echo "Version DB regression tests passed"