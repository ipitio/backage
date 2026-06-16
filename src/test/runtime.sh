#!/bin/bash

# Test doubles are invoked indirectly by functions sourced from production.
# Subshell-local runtime settings are intentionally separate from later tests.
# shellcheck disable=SC1091,SC2030,SC2031,SC2034,SC2317

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
src_dir=${src_dir:?}
workdir=${workdir:?}

test_sqlite_retries_transient_write_failure() {
	local fake_bin="$workdir/fake-sqlite-bin"
	local fake_sqlite="$fake_bin/sqlite3"
	local attempts_file="$workdir/sqlite-attempts.txt"
	local original_path="$PATH"
	local result
	local original_bkg_env="${BKG_ENV:-}"

	mkdir -p "$fake_bin"
	cat >"$fake_sqlite" <<'EOF'
#!/bin/bash

attempts=0
[ ! -f "$TEST_SQLITE_ATTEMPTS_FILE" ] || attempts=$(cat "$TEST_SQLITE_ATTEMPTS_FILE")
attempts=$((attempts + 1))
printf '%s\n' "$attempts" >"$TEST_SQLITE_ATTEMPTS_FILE"

if [ "$attempts" -eq 1 ]; then
	echo 'database is locked' >&2
	exit 1
fi

printf 'ok\n'
EOF
	chmod +x "$fake_sqlite"

	TEST_SQLITE_ATTEMPTS_FILE="$attempts_file"
	export TEST_SQLITE_ATTEMPTS_FILE
	BKG_ENV="$workdir/env-sqlite.env"
	: >"$BKG_ENV"
	PATH="$fake_bin:$original_path"
	BKG_SQLITE_MAX_ATTEMPTS=3
	BKG_SQLITE_RETRY_DELAY_SECS=1
	result=$(sqlite3 "$workdir/test.db" "insert into demo values (1);")
	PATH="$original_path"
	BKG_ENV="$original_bkg_env"

	[ "$result" = "ok" ] || fail "Expected sqlite3 wrapper to return retried output"
	[ "$(cat "$attempts_file")" -eq 2 ] || fail "Expected sqlite3 wrapper to retry a transient write failure once"
}

test_sqlite_ensure_index_schema_adds_query_indexes() {
	local original_db="${BKG_INDEX_DB:-}"
	local indexes

	BKG_INDEX_DB="$workdir/schema-indexes.db"
	sqlite_ensure_index_schema >/dev/null
	indexes=$(command sqlite3 "$BKG_INDEX_DB" "select name from sqlite_master where type='index' order by name;")
	grep -Fq 'idx_bkg_owners_date_owner' <<<"$indexes" || fail "Expected owners date index"
	grep -Fq 'idx_bkg_packages_owner_repo_package_date' <<<"$indexes" || fail "Expected owner/repo/package package index"
	grep -Fq 'idx_bkg_packages_owner_date_downloads' <<<"$indexes" || fail "Expected owner downloads package index"
	grep -Fq 'idx_bkg_packages_owner_repo_date_downloads' <<<"$indexes" || fail "Expected repo downloads package index"
	grep -Fq 'idx_bkg_versions_package_date' <<<"$indexes" || fail "Expected normalized versions package index"
	grep -Fq 'idx_bkg_versions_date' <<<"$indexes" || fail "Expected normalized versions date index"
	BKG_INDEX_DB="$original_db"
}

test_batch_reset_uses_explicit_progress_only() {
	batch_should_reset 0 || fail "Expected exhausted work queue to reset"
	! batch_should_reset 35815 || fail "Database allocation stability must not reset an active batch"
	! batch_should_reset 500 || fail "A completion-count threshold must not reset an unfinished batch"
	! batch_should_reset 1 || fail "A small nonempty tail must remain eligible for completion"
}

test_sqlite_numeric_version_ids_use_builtin_glob() {
	local rows

	rows=$(sqlite3 ':memory:' "
		with ids(id) as (
			values ('1'), ('001'), ('1a'), ('a1'), (''), ('12-3')
		)
		select id || '|' || (
			case
				when id != '' and id not glob '*[^0-9]*' then cast(id as integer)
				else 'not-numeric'
			end
		)
		from ids;
	")

	[ "$rows" = $'1|1\n001|1\n1a|not-numeric\na1|not-numeric\n|not-numeric\n12-3|not-numeric' ] || fail "Expected stock SQLite GLOB to classify numeric version IDs without a REGEXP extension"
}

test_cleanup_generated_json_sidecars_removes_adaptive_retry_artifacts() {
	local sidecar_dir="$workdir/sidecar-cleanup"

	mkdir -p "$sidecar_dir/repo"
	printf '{}\n' >"$sidecar_dir/repo/package.json"
	printf 'tmp\n' >"$sidecar_dir/repo/package.json.tmp"
	printf 'abs\n' >"$sidecar_dir/repo/package.json.abs"
	printf 'rel\n' >"$sidecar_dir/repo/package.json.rel"
	printf 'best\n' >"$sidecar_dir/repo/..json.tmp.best.xuScLK"
	printf 'try\n' >"$sidecar_dir/repo/..json.tmp.try.LwARKH"
	printf 'abs retry\n' >"$sidecar_dir/repo/package.json.abs.retry"
	printf 'rel retry\n' >"$sidecar_dir/repo/package.json.rel.retry"

	cleanup_generated_json_sidecars "$sidecar_dir"

	assert_file_exists "$sidecar_dir/repo/package.json"
	[ ! -e "$sidecar_dir/repo/package.json.tmp" ] || fail "Expected .json.tmp sidecar to be removed"
	[ ! -e "$sidecar_dir/repo/package.json.abs" ] || fail "Expected .json.abs sidecar to be removed"
	[ ! -e "$sidecar_dir/repo/package.json.rel" ] || fail "Expected .json.rel sidecar to be removed"
	[ ! -e "$sidecar_dir/repo/..json.tmp.best.xuScLK" ] || fail "Expected adaptive best sidecar to be removed"
	[ ! -e "$sidecar_dir/repo/..json.tmp.try.LwARKH" ] || fail "Expected adaptive try sidecar to be removed"
	[ ! -e "$sidecar_dir/repo/package.json.abs.retry" ] || fail "Expected .json.abs.* sidecar to be removed"
	[ ! -e "$sidecar_dir/repo/package.json.rel.retry" ] || fail "Expected .json.rel.* sidecar to be removed"
}

test_drop_replaced_legacy_version_tables_keeps_unreplaced_fallbacks() {
	local original_db="${BKG_INDEX_DB:-}"
	local today="2026-03-30"
	local yesterday="2026-03-29"
	local replaced_table="versions_orgs_container_Lazztech_Repo_With_Underscore_libre_pkg"
	local unreplaced_table="versions_orgs_container_Lazztech_OtherRepo_other_pkg"
	local orphan_table="versions_orgs_container_Lazztech_OrphanRepo_orphan_pkg"
	local table_count
	local stale_count

	BKG_INDEX_DB="$workdir/legacy-version-cleanup.db"
	sqlite_ensure_index_schema >/dev/null
	sqlite3 "$BKG_INDEX_DB" "create table '$replaced_table' (id text not null, name text not null, size integer not null, downloads integer not null, downloads_month integer not null, downloads_week integer not null, downloads_day integer not null, date text not null, tags text, primary key (id, date));"
	sqlite3 "$BKG_INDEX_DB" "create table '$unreplaced_table' (id text not null, name text not null, size integer not null, downloads integer not null, downloads_month integer not null, downloads_week integer not null, downloads_day integer not null, date text not null, tags text, primary key (id, date));"
	sqlite3 "$BKG_INDEX_DB" "create table '$orphan_table' (id text not null, name text not null, size integer not null, downloads integer not null, downloads_month integer not null, downloads_week integer not null, downloads_day integer not null, date text not null, tags text, primary key (id, date));"

	sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('69664378','orgs','container','Lazztech','Repo_With_Underscore','libre_pkg','2000','300','200','20','400','$today');"
	sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('69664378','orgs','container','Lazztech','OtherRepo','other_pkg','1000','100','50','5','300','$today');"

	sqlite3 "$BKG_INDEX_DB" "insert into '$replaced_table' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('10','sha256:b','456','985','985','455','3','$today','latest');"
	sqlite3 "$BKG_INDEX_DB" "insert into '$unreplaced_table' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('20','sha256:c','222','111','22','11','1','$today','latest');"
	sqlite3 "$BKG_INDEX_DB" "insert into '$unreplaced_table' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('19','sha256:old','111','10','10','10','1','$yesterday','old');"
	sqlite3 "$BKG_INDEX_DB" "insert into '$orphan_table' (id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('30','sha256:d','222','111','22','11','1','$today','latest');"
	sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_VER' (owner_id, owner_type, package_type, owner, repo, package, id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('69664378','orgs','container','Lazztech','Repo_With_Underscore','libre_pkg','10','sha256:b','456','985','985','455','3','$today','latest');"

	drop_replaced_legacy_version_tables "$today"

	table_count=$(sqlite3 "$BKG_INDEX_DB" "select count(*) from sqlite_master where type='table' and name=$(sqlite_quote_literal "$replaced_table");")
	[ "$table_count" = "0" ] || fail "Expected replaced legacy version table to be dropped during cleanup"
	table_count=$(sqlite3 "$BKG_INDEX_DB" "select count(*) from sqlite_master where type='table' and name=$(sqlite_quote_literal "$unreplaced_table");")
	[ "$table_count" = "1" ] || fail "Expected unreplaced legacy version table to remain as fallback"
	table_count=$(sqlite3 "$BKG_INDEX_DB" "select count(*) from sqlite_master where type='table' and name=$(sqlite_quote_literal "$orphan_table");")
	[ "$table_count" = "0" ] || fail "Expected orphaned legacy version table to be dropped during cleanup"
	stale_count=$(sqlite3 "$BKG_INDEX_DB" "select count(*) from '$unreplaced_table' where date < $(sqlite_quote_literal "$today");")
	[ "$stale_count" = "0" ] || fail "Expected stale rows in kept legacy version tables to be pruned"

	BKG_INDEX_DB="$original_db"
}

test_parallel_async_wait_continues_after_non_timeout_failure() {
	local status=0
	local completed_file="$workdir/parallel-async-completed.txt"

	failing_async_worker() {
		return 1
	}

	succeeding_async_worker() {
		printf 'done\n' >>"$completed_file"
	}

	BKG_ENV="$workdir/env-parallel-async-continue.env"
	: >"$BKG_ENV"
	: >"$completed_file"

	parallel_async_submit failing_async_worker "one"
	parallel_async_submit succeeding_async_worker "two"

	if parallel_async_wait; then
		fail "Expected parallel_async_wait to surface a non-timeout worker failure"
	else
		status=$?
	fi

	[ "$status" -eq 1 ] || fail "Expected parallel_async_wait to return 1, got $status"
	assert_contains "$completed_file" "done"
	unset -f failing_async_worker
	unset -f succeeding_async_worker
}

test_parallel_async_default_max_jobs_is_tuned() {
	local default_jobs
	local expected_jobs
	local override_jobs

	unset BKG_PARALLEL_ASYNC_MAX_JOBS
	expected_jobs=$(( $(command nproc --all) * 2 ))
	default_jobs=$(parallel_async_default_max_jobs)
	BKG_PARALLEL_ASYNC_MAX_JOBS=9
	override_jobs=$(parallel_async_default_max_jobs)

	[ "$default_jobs" = "$expected_jobs" ] || fail "Expected tuned default async max jobs to be $expected_jobs, got $default_jobs"
	[ "$override_jobs" = "9" ] || fail "Expected explicit async max jobs override to be honored, got $override_jobs"

	unset BKG_PARALLEL_ASYNC_MAX_JOBS
}

test_parallel_shell_func_preserves_inherited_runtime_config() {
	local fixture_file="$workdir/worker-config.sh"
	local input_file="$workdir/worker-config-input.txt"
	local output_file="$workdir/worker-config-output.txt"
	local original_env_file="${BKG_ENV:-}"
	local original_mode="${BKG_MODE:-}"
	local original_max_len="${BKG_MAX_LEN:-}"
	local original_page_all="${BKG_PAGE_ALL:-}"
	local original_owners="${BKG_OWNERS:-}"

	cat >"$fixture_file" <<EOF
#!/bin/bash

	source "$src_dir/lib/util.sh"

worker_emit_runtime_config() {
	printf '%s|%s|%s|%s|%s\n' "\$BKG_MODE" "\$BKG_MAX_LEN" "\$BKG_PAGE_ALL" "\$BKG_ENV" "\$BKG_OWNERS"
}
EOF

	printf 'one\n' >"$input_file"
	BKG_ENV="$workdir/custom-worker.env"
	: >"$BKG_ENV"
	BKG_MODE=5
	BKG_MAX_LEN=123
	BKG_PAGE_ALL=0
	BKG_OWNERS="$workdir/custom-owners.txt"
	: >"$BKG_OWNERS"

	parallel_shell_func "$fixture_file" worker_emit_runtime_config --lb <"$input_file" >"$output_file"

	assert_contains "$output_file" "5|123|0|$workdir/custom-worker.env|$workdir/custom-owners.txt"

	BKG_ENV="$original_env_file"
	BKG_MODE="$original_mode"
	BKG_MAX_LEN="$original_max_len"
	BKG_PAGE_ALL="$original_page_all"
	BKG_OWNERS="$original_owners"
}

test_direct_bash_child_preserves_inherited_runtime_config() {
	local fixture_file="$workdir/direct-child-config.sh"
	local output_file="$workdir/direct-child-config-output.txt"

	cat >"$fixture_file" <<EOF
#!/bin/bash

	source "$src_dir/lib/util.sh"
printf '%s|%s|%s|%s|%s\n' "\$BKG_MODE" "\$BKG_MAX_LEN" "\$BKG_PAGE_ALL" "\$BKG_ENV" "\$BKG_OWNERS"
EOF

	BKG_ENV="$workdir/direct-child.env"
	: >"$BKG_ENV"
	BKG_MODE=4
	BKG_MAX_LEN=321
	BKG_PAGE_ALL=0
	BKG_OWNERS="$workdir/direct-child-owners.txt"
	: >"$BKG_OWNERS"

	bash "$fixture_file" >"$output_file"

	assert_contains "$output_file" "4|321|0|$workdir/direct-child.env|$workdir/direct-child-owners.txt"
}

test_util_default_env_path_is_absolute() {
	local fixture_file="$workdir/default-env-path.sh"
	local output_file="$workdir/default-env-path.out"

	cat >"$fixture_file" <<EOF
#!/bin/bash

unset BKG_ENV
	source "$src_dir/lib/util.sh"
printf '%s\n' "\$BKG_ENV"
EOF

	bash "$fixture_file" >"$output_file"
	assert_contains "$output_file" "$src_dir/env.env"
}

test_bkg_python_forwards_unexported_http_settings() {
	local fake_python="$workdir/fake-python"
	local output_file="$workdir/python-http-env.out"

	cat >"$fake_python" <<'EOF'
#!/bin/bash
printf '%s|%s|%s\n' "$GITHUB_TOKEN" "$BKG_HTTP_TOTAL_TIMEOUT" "$*"
EOF
	chmod +x "$fake_python"

	(
		GITHUB_TOKEN="local-token"
		BKG_HTTP_TOTAL_TIMEOUT=45
		export -n GITHUB_TOKEN BKG_HTTP_TOTAL_TIMEOUT
		BKG_PYTHON="$fake_python"
		bkg_python github rest users/ipitio
	) >"$output_file"

	assert_contains "$output_file" "local-token|45|-m bkg_py github rest users/ipitio"
}

test_ensure_pages_dotfiles_visible_writes_nojekyll() {
	local site_root="$workdir/pages-site"

	ensure_pages_dotfiles_visible "$site_root" || fail "Expected ensure_pages_dotfiles_visible to succeed"
	assert_file_exists "$site_root/.nojekyll"
}

test_update_version_logs_sqlite_write_failure() {
	local row
	local status=0

	row=$(printf '%s' '{"id":747026466,"name":"sha256:test","tags":"latest"}' | base64 -w0)

	if (
		BKG_ENV="$workdir/env-update-version.env"
		: >"$BKG_ENV"
		now=$(date -u +%s)
		set_BKG BKG_SCRIPT_START "$now"
		set_BKG BKG_RATE_LIMIT_START "$now"
		set_BKG BKG_MIN_RATE_LIMIT_START "$now"
		set_BKG BKG_CALLS_TO_API 0
		set_BKG BKG_MIN_CALLS_TO_API 0
		BKG_INDEX_DB="$workdir/update-version.db"
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
		update_version "$row"
		BKG_INDEX_DB="$workdir/unopenable-db"
		mkdir -p "$BKG_INDEX_DB"
		version_flush_staged_rows
	) >/dev/null 2>&1; then
		fail "Expected version_flush_staged_rows to return non-zero when the SQLite write fails"
	else
		status=$?
	fi

	[ "$status" -eq 1 ] || fail "Expected version_flush_staged_rows to return 1, got $status"
}

test_update_package_warns_on_package_level_fallback() {
	local output_file="$workdir/update-package-fallback.log"
	local json_file="$workdir/index/Lazztech/Libre-Closet/libre-closet.json"

	if ! (
		cd "$workdir"
		BKG_ENV="$workdir/env-update-package.env"
		: >"$BKG_ENV"
		now=$(date -u +%s)
		set_BKG BKG_SCRIPT_START "$now"
		set_BKG BKG_RATE_LIMIT_START "$now"
		set_BKG BKG_MIN_RATE_LIMIT_START "$now"
		set_BKG BKG_CALLS_TO_API 0
		set_BKG BKG_MIN_CALLS_TO_API 0
		BKG_INDEX_DB="$workdir/test.db"
		BKG_INDEX_DIR="$workdir/index"
		BKG_OPTOUT="$workdir/optout.txt"
		: >"$BKG_OPTOUT"
		BKG_OWNERS="$workdir/owners.txt"
		: >"$BKG_OWNERS"
		BKG_BATCH_FIRST_STARTED='2026-04-02'
		owner_id=69664378
		owner='Lazztech'
		owner_type='orgs'
		repo='Libre-Closet'
		package='libre-closet'
		package_type='container'
		lower_owner='lazztech'
		lower_package='libre-closet'
		fast_out=false
		BKG_MODE=0
		sqlite3 "$BKG_INDEX_DB" "create table if not exists '$BKG_INDEX_TBL_PKG' (owner_id text, owner_type text not null, package_type text not null, owner text not null, repo text not null, package text not null, downloads integer not null, downloads_month integer not null, downloads_week integer not null, downloads_day integer not null, size integer not null, date text not null, primary key (owner_id, package, date));"
		sqlite3 "$BKG_INDEX_DB" "create table if not exists 'versions_orgs_container_Lazztech_Libre-Closet_libre-closet' (id text not null, name text not null, size integer not null, downloads integer not null, downloads_month integer not null, downloads_week integer not null, downloads_day integer not null, date text not null, tags text, primary key (id, date));"
		sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('69664378','orgs','container','Lazztech','Libre-Closet','libre-closet','2394','-1','-1','-1','-1','2026-04-02');"
		printf '69664378|Lazztech|Libre-Closet|libre-closet|2026-04-02\n' >packages_already_updated
		update_package 'container/Libre-Closet/libre-closet'
	) >"$output_file" 2>&1; then
		fail "Expected update_package to continue and emit fallback JSON when version rows are missing"
	fi

	assert_contains "$output_file" "No version rows available for Lazztech/libre-closet; using package-level fallback data"
	assert_file_exists "$json_file"
	jq -e '.raw_versions == 0 and .raw_downloads == 2394 and (.version | length) == 1 and .version[0].id == -1' "$json_file" >/dev/null || fail "Expected fallback package JSON when version rows are missing"
}

test_run_owner_page_discovery_stops_on_code_2() {
	local calls_file="$workdir/owner-pages.txt"

	page_owner() {
		printf '%s\n' "$1" >>"$calls_file"
		[ "$1" -lt 1 ] && return 0
		return 2
	}

	run_owner_page_discovery || fail "Expected run_owner_page_discovery to treat exit code 2 as normal completion"

	assert_contains "$calls_file" "1"
	[ "$(wc -l <"$calls_file")" -eq 1 ] || fail "Expected run_owner_page_discovery to stop immediately after the first page_owner exit code 2"
	unset -f page_owner
}

test_run_owner_page_discovery_caps_at_one_page() {
	local calls_file="$workdir/owner-pages-max.txt"

	page_owner() {
		printf '%s\n' "$1" >>"$calls_file"
		return 0
	}

	run_owner_page_discovery || fail "Expected run_owner_page_discovery to stop cleanly after the maximum number of pages"

	assert_contains "$calls_file" "1"
	[ "$(wc -l <"$calls_file")" -eq 1 ] || fail "Expected run_owner_page_discovery to cap owner discovery at one page"
	unset -f page_owner
}

test_daily_gate_helpers_track_per_day() {
	BKG_ENV="$workdir/env-daily-gate.env"
	: >"$BKG_ENV"
	set_BKG BKG_BATCH_MARKER batch-1
	set_BKG BKG_REST_TO_TOP 0

	if daily_gate_completed_today BKG_LAST_EXPLORE_DATE 2026-04-10; then
		fail "Expected daily gate to be incomplete before it is marked"
	fi

	mark_daily_gate_completed BKG_LAST_EXPLORE_DATE 2026-04-10
	daily_gate_completed_today BKG_LAST_EXPLORE_DATE 2026-04-10 || fail "Expected daily gate to be complete for the marked day"

	if daily_gate_completed_today BKG_LAST_EXPLORE_DATE 2026-04-11; then
		fail "Expected daily gate to be incomplete for a different day"
	fi
}

test_daily_gate_skip_depends_on_master_commit_today() {
	BKG_ENV="$workdir/env-daily-gate-skip.env"
	: >"$BKG_ENV"
	set_BKG BKG_BATCH_MARKER batch-1
	set_BKG BKG_REST_TO_TOP 0
	mark_daily_gate_completed BKG_LAST_EXPLORE_DATE 2026-04-10

	master_branch_has_commit_today() {
		[ "$1" = "2026-04-10" ] || fail "Expected skip helper to pass the current day into master_branch_has_commit_today"
		return 1
	}

	if daily_gate_should_skip_today BKG_LAST_EXPLORE_DATE 2026-04-10; then
		fail "Expected daily gate skip to stay disabled when master has no commit today"
	fi

	unset -f master_branch_has_commit_today

	master_branch_has_commit_today() {
		return 0
	}

	daily_gate_should_skip_today BKG_LAST_EXPLORE_DATE 2026-04-10 || fail "Expected daily gate skip to activate once master has a commit today"
	unset -f master_branch_has_commit_today
}

test_daily_gate_skip_resets_on_new_batch_marker() {
	BKG_ENV="$workdir/env-daily-gate-batch.env"
	: >"$BKG_ENV"
	set_BKG BKG_BATCH_MARKER batch-1
	set_BKG BKG_REST_TO_TOP 0
	mark_daily_gate_completed BKG_LAST_EXPLORE_DATE 2026-04-10

	master_branch_has_commit_today() {
		return 0
	}

	daily_gate_should_skip_today BKG_LAST_EXPLORE_DATE 2026-04-10 || fail "Expected daily gate skip to apply for the current batch marker"
	set_BKG BKG_BATCH_MARKER batch-2
	if daily_gate_should_skip_today BKG_LAST_EXPLORE_DATE 2026-04-10; then
		fail "Expected daily gate skip to be cleared after the batch marker changes"
	fi
	unset -f master_branch_has_commit_today
}

test_daily_gate_skip_resets_on_rest_to_top_change() {
	BKG_ENV="$workdir/env-daily-gate-rest.env"
	: >"$BKG_ENV"
	set_BKG BKG_BATCH_MARKER batch-1
	set_BKG BKG_REST_TO_TOP 0
	mark_daily_gate_completed BKG_LAST_EXPLORE_DATE 2026-04-10

	master_branch_has_commit_today() {
		return 0
	}

	daily_gate_should_skip_today BKG_LAST_EXPLORE_DATE 2026-04-10 || fail "Expected daily gate skip to apply for the current BKG_REST_TO_TOP value"
	set_BKG BKG_REST_TO_TOP 1
	if daily_gate_should_skip_today BKG_LAST_EXPLORE_DATE 2026-04-10; then
		fail "Expected daily gate skip to be cleared after BKG_REST_TO_TOP changes"
	fi
	unset -f master_branch_has_commit_today
}

test_check_limit_retries_missing_script_start_once() {
	local now
	local reads_file="$workdir/check-limit-reads.txt"

	now=$(date -u +%s)
	if ! (
		BKG_ENV="$workdir/env-check-limit.env"
		: >"$BKG_ENV"
		BKG_MAX_LEN=3600
		printf '0\n' >"$reads_file"

		get_BKG() {
			local script_start_reads
			case "$1" in
			BKG_SCRIPT_START)
				script_start_reads=$(cat "$reads_file")
				script_start_reads=$((script_start_reads + 1))
				printf '%s\n' "$script_start_reads" >"$reads_file"
				if [ "$script_start_reads" -eq 1 ]; then
					printf ''
				else
					printf '%s\n' "$now"
				fi
				;;
			BKG_CALLS_TO_API|BKG_MIN_CALLS_TO_API)
				printf '0\n'
				;;
			BKG_RATE_LIMIT_START|BKG_MIN_RATE_LIMIT_START)
				printf '%s\n' "$now"
				;;
			esac
		}

		check_limit >/dev/null
	); then
		fail "Expected check_limit to succeed when the second BKG_SCRIPT_START read succeeds"
	fi
	[ "$(cat "$reads_file")" -eq 2 ] || fail "Expected check_limit to retry BKG_SCRIPT_START exactly once"
}

test_query_graphql_api_delegates_query_to_python() {
	local response

	init_bkg_runtime_state "$workdir/env-graphql.env"
	GITHUB_TOKEN=dummy

	bkg_python() {
		[ "$1" = "github" ] || fail "Expected query_graphql_api to use the GitHub Python command"
		[ "$2" = "graphql" ] || fail "Expected query_graphql_api to use the GraphQL Python command"
		[ "$(cat)" = 'query { viewer { login } }' ] || fail "Expected query_graphql_api to pass the original query on standard input"
		cat <<'EOF'
{"data":{"viewer":{"login":"ipitio"},"rateLimit":{"cost":17,"remaining":4321,"resetAt":"2026-04-10T23:59:59Z"}}}
EOF
	}

	response=$(query_graphql_api 'query { viewer { login } }')

	jq -e '.data.viewer.login == "ipitio"' <<<"$response" >/dev/null || fail "Expected query_graphql_api to preserve Python JSON output"
	unset -f bkg_python
	GITHUB_TOKEN=""
}

test_query_api_delegates_path_to_python() {
	local response

	init_bkg_runtime_state "$workdir/env-rest.env"
	GITHUB_TOKEN=dummy

	bkg_python() {
		[ "$1" = "github" ] || fail "Expected query_api to use the GitHub Python command"
		[ "$2" = "rest" ] || fail "Expected query_api to use the REST Python command"
		[ "$3" = "users/ipitio" ] || fail "Expected query_api to preserve the REST path"
		printf '%s\n' '{"login":"ipitio"}'
	}

	response=$(query_api "users/ipitio")

	jq -e '.login == "ipitio"' <<<"$response" >/dev/null || fail "Expected query_api to preserve Python JSON output"
	unset -f bkg_python
	GITHUB_TOKEN=""
}

test_query_api_optional_allows_only_missing_responses() {
	local response

	init_bkg_runtime_state "$workdir/env-rest-optional.env"
	GITHUB_TOKEN=dummy

	bkg_python() {
		[ "$1" = "github" ] || fail "Expected query_api_optional to use the GitHub Python command"
		[ "$2" = "rest" ] || fail "Expected query_api_optional to use the REST Python command"
		[ "$3" = "users/missing" ] || fail "Expected query_api_optional to preserve the REST path"
		[ "$4" = "--missing-ok" ] || fail "Expected query_api_optional to allow HTTP 404 explicitly"
		printf '%s\n' 'null'
	}

	response=$(query_api_optional "users/missing")

	[ "$response" = "null" ] || fail "Expected query_api_optional to preserve the null response"
	unset -f bkg_python
	GITHUB_TOKEN=""
}

test_dldb_delegates_release_snapshot_download_to_python() {
	local calls_file="$workdir/dldb-python-calls.txt"
	local original_root="${BKG_ROOT:-}"
	local original_index_db="${BKG_INDEX_DB:-}"

	BKG_ROOT="$workdir/dldb-root"
	BKG_INDEX_DB="$BKG_ROOT/index.db"
	mkdir -p "$BKG_ROOT"
	: >"$calls_file"

	bkg_python() {
		printf '%s\n' "$*" >>"$calls_file"
		[ "$1" = "snapshot" ] || fail "Expected dldb to use the snapshot Python command"
		[ "$2" = "download-release" ] || fail "Expected dldb to use the release download command"
		[ "$3" = "v2026.6.0" ] || fail "Expected dldb to preserve the requested release tag"
		return 0
	}

	dldb "v2026.6.0" >/dev/null

	assert_contains "$calls_file" "snapshot download-release v2026.6.0"
	assert_contains "$BKG_ROOT/.gitignore" "*.db*"

	unset -f bkg_python
	BKG_ROOT="$original_root"
	BKG_INDEX_DB="$original_index_db"
}

test_dldb_check_mode_uses_python_release_metadata_probe() {
	local calls_file="$workdir/dldb-python-check-calls.txt"

	: >"$calls_file"
	bkg_python() {
		printf '%s\n' "$*" >>"$calls_file"
		return 0
	}

	dldb "v2026.6.0" check >/dev/null

	assert_contains "$calls_file" "snapshot download-release v2026.6.0 --check"
	unset -f bkg_python
}

test_resolve_release_snapshot_asset_rejects_http_errors() {
	local calls_file="$workdir/release-asset-probe-calls.txt"

	BKG_INDEX_DB="$workdir/index.db"
	: >"$calls_file"

	curl() {
		printf '%s\n' "$*" >>"$calls_file"
		printf '403\n'
	}

	if resolve_release_snapshot_asset "v2026.6.0" >/dev/null; then
		fail "Expected HTTP errors to be rejected as missing release assets"
	fi
	[ "$(wc -l <"$calls_file")" -eq 3 ] || fail "Expected every supported snapshot name to be probed"
}

test_check_db_deletes_missing_release_despite_stale_runtime_state() {
	local calls_file="$workdir/check-db-calls.txt"
	local latest_calls_file="$workdir/check-db-latest-calls.txt"

	BKG_ENV="$workdir/env-check-db.env"
	: >"$BKG_ENV"
	set_BKG BKG_SCRIPT_START "$(( $(date -u +%s) - BKG_MAX_LEN - 1 ))"
	set_BKG BKG_TIMEOUT "1"
	BKG_INDEX_DB="$workdir/index.db"
	GITHUB_OWNER="ipitio"
	GITHUB_REPO="backage"
	: >"$calls_file"
	printf '0\n' >"$latest_calls_file"

	curl_gh_direct() {
		local latest_calls

		printf '%s\n' "$*" >>"$calls_file"
		if [[ " $* " == *" -X DELETE "* ]]; then
			return 0
		fi

		latest_calls=$(cat "$latest_calls_file")
		latest_calls=$((latest_calls + 1))
		printf '%s\n' "$latest_calls" >"$latest_calls_file"
		if [ "$latest_calls" -eq 1 ]; then
			printf '%s\n' '{"id":332248762,"tag_name":"v2026.6.0","assets":[]}'
		else
			printf '%s\n' '{"id":331227112,"tag_name":"v2026.5.2","assets":[{"name":"index.db"}]}'
		fi
	}

	check_db >/dev/null

	[ "$(cat "$latest_calls_file")" -eq 2 ] || fail "Expected check_db to verify the release after deletion"
	assert_contains "$calls_file" "releases/332248762"
}

test_check_db_reports_delete_failure() {
	local output_file="$workdir/check-db-delete-failure.txt"

	BKG_INDEX_DB="$workdir/index.db"
	GITHUB_OWNER="ipitio"
	GITHUB_REPO="backage"

	curl_gh_direct() {
		if [[ " $* " == *" -X DELETE "* ]]; then
			return 22
		fi
		printf '%s\n' '{"id":332248762,"tag_name":"v2026.6.0","assets":[]}'
	}

	if check_db >"$output_file" 2>&1; then
		fail "Expected check_db to fail when GitHub rejects release deletion"
	fi
	assert_contains "$output_file" "Failed to delete latest release v2026.6.0"
}

test_resolve_owner_ids_uses_run_cache_before_live_lookup() {
	local candidates_file="$workdir/owner-candidates-cache.txt"
	local output_file="$workdir/owner-ids-cache.txt"

	BKG_ENV="$workdir/env-owner-cache.env"
	: >"$BKG_ENV"
	reset_owner_id_cache
	cache_owner_ref '200/beta'
	cache_owner_ref '300/gamma'

	cat >"$candidates_file" <<'EOF'
beta
gamma
EOF

	query_graphql_api() {
		fail "Expected resolve_owner_ids to use the run-scoped cache before GraphQL"
	}

	owner_get_id() {
		fail "Expected resolve_owner_ids to use the run-scoped cache before owner_get_id fallback"
	}

	resolve_owner_ids "$candidates_file" >"$output_file"

	assert_contains "$output_file" "200/beta"
	assert_contains "$output_file" "300/gamma"
	unset -f query_graphql_api
	unset -f owner_get_id
}

test_resolve_owner_ids_preserves_ids_and_records_graphql_misses() {
	local candidates_file="$workdir/owner-candidates.txt"
	local output_file="$workdir/owner-ids.txt"
	local missing_file="$workdir/missing-owner-ids.txt"
	local query_log="$workdir/graphql-query.txt"

	cat >"$candidates_file" <<'EOF'
123/alpha
beta
0/gamma
delta
EOF

	BKG_ENV="$workdir/env-owner-batch.env"
	: >"$BKG_ENV"
	reset_owner_id_cache
	GITHUB_TOKEN=dummy

	query_graphql_api() {
		printf '%s\n' "$1" >"$query_log"
		cat <<'EOF'
{"data":{"o0":{"login":"Beta","databaseId":200},"o1":{"login":"gamma","databaseId":300},"o2":null}}
EOF
	}

	owner_get_id() {
		fail "Expected a successful GraphQL null to avoid redundant REST and HTML probes"
	}

	resolve_owner_ids "$candidates_file" "$missing_file" >"$output_file"

	assert_contains "$output_file" "123/alpha"
	assert_contains "$output_file" "200/Beta"
	assert_contains "$output_file" "300/gamma"
	assert_contains "$missing_file" "delta"
	assert_contains "$query_log" 'repositoryOwner(login:"beta")'
	assert_contains "$query_log" 'repositoryOwner(login:"gamma")'
	assert_contains "$query_log" 'repositoryOwner(login:"delta")'
	unset -f query_graphql_api
	unset -f owner_get_id
	GITHUB_TOKEN=""
}

test_resolve_owner_ids_falls_back_when_graphql_fails() {
	local candidates_file="$workdir/owner-candidates-fallback.txt"
	local output_file="$workdir/owner-ids-fallback.txt"
	local missing_file="$workdir/missing-owner-ids-fallback.txt"

	printf '%s\n' fallback >"$candidates_file"
	BKG_ENV="$workdir/env-owner-fallback.env"
	: >"$BKG_ENV"
	reset_owner_id_cache
	GITHUB_TOKEN=dummy

	query_graphql_api() {
		return 1
	}

	owner_get_id() {
		[ "$1" = "fallback" ] || fail "Expected the unresolved owner to use the fallback"
		printf '%s\n' '400/fallback'
	}

	resolve_owner_ids "$candidates_file" "$missing_file" >"$output_file"

	assert_contains "$output_file" "400/fallback"
	[ ! -s "$missing_file" ] || fail "Expected a failed GraphQL request not to mark the owner missing"
	unset -f query_graphql_api
	unset -f owner_get_id
	GITHUB_TOKEN=""
}

test_retire_missing_owner_removes_sparse_tree_and_manual_entry() {
	local index_repo="$workdir/retire-index"
	local database_calls="$workdir/retire-database-calls.txt"

	BKG_INDEX_DIR="$index_repo"
	BKG_OWNERS="$workdir/retire-owners.txt"
	mkdir -p "$index_repo/missing/repo"
	printf '%s\n' '[]' >"$index_repo/missing/repo/.json"
	git -C "$index_repo" init -q
	git -C "$index_repo" config user.name test
	git -C "$index_repo" config user.email test@example.com
	git -C "$index_repo" add .
	git -C "$index_repo" commit -qm init
	printf '%s\n' missing keep >"$BKG_OWNERS"
	: >"$database_calls"

	bkg_python() {
		printf '%s\n' "$*" >>"$database_calls"
	}

	retire_missing_owner missing >/dev/null

	[ ! -e "$index_repo/missing" ] || fail "Expected the unavailable owner's generated tree to be removed"
	! grep -Fxq missing "$BKG_OWNERS" || fail "Expected the unavailable owner to be removed from owners.txt"
	grep -Fxq keep "$BKG_OWNERS" || fail "Expected unrelated owners.txt entries to remain"
	assert_contains "$database_calls" "database retire-owner missing"
	git -C "$index_repo" diff --cached --quiet && fail "Expected sparse owner retirement to stage the tree deletion"
	unset -f bkg_python
}

test_restore_db_from_snapshot_skips_when_signature_matches() {
	local db_root="$workdir/db-restore-skip"
	local output_file="$db_root/output.txt"

	mkdir -p "$db_root"
	BKG_INDEX_DB="$db_root/index.db"
	mkdir -p "$(dirname "$(db_snapshot_archive_file)")"
	printf '%s\n' 'snapshot-data' >"$(db_snapshot_archive_file)"
	printf '%s\n' 'existing-db' >"$BKG_INDEX_DB"
	current_index_snapshot_signature >"$(db_restore_signature_file)"

	unzstd() {
		fail "Expected matching snapshot signature to skip database restore"
	}

	restore_db_from_index_snapshot_if_needed >"$output_file"

	unset -f unzstd
	assert_contains "$output_file" "Using existing database; index.db unchanged"
	assert_contains "$BKG_INDEX_DB" "existing-db"
}

test_snapshot_helpers_use_python_archive_selection() {
	local db_root="$workdir/snapshot-helper-selection"
	local selected
	local expected_signature

	mkdir -p "$db_root"
	BKG_INDEX_DB="$db_root/index.db"
	BKG_INDEX_SQL="$db_root/index.sql"
	printf '%s\n' 'legacy-db' >"$(legacy_db_snapshot_archive_file)"

	selected=$(current_index_snapshot_archive_file)
	[ "$selected" = "$(legacy_db_snapshot_archive_file)" ] ||
		fail "Expected legacy DB archive to be selected when current snapshot is absent"

	mkdir -p "$(dirname "$(db_snapshot_archive_file)")"
	printf '%s\n' 'current-db' >"$(db_snapshot_archive_file)"
	selected=$(current_index_snapshot_archive_file)
	[ "$selected" = "$(db_snapshot_archive_file)" ] ||
		fail "Expected current DB archive to take precedence over legacy snapshots"

	expected_signature=$(sha256sum "$(db_snapshot_archive_file)" | awk '{print $1}')
	[ "$(current_index_snapshot_signature)" = "$expected_signature" ] ||
		fail "Expected Python snapshot signature to match sha256sum"
}

test_snapshot_path_helpers_use_python_derivation() {
	local db_root="$workdir/snapshot-helper-paths"

	mkdir -p "$db_root"
	BKG_INDEX_DB="$db_root/index.db"
	BKG_INDEX_SQL="$db_root/index.sql"

	[ "$(db_snapshot_archive_file)" = "$db_root/.snapshot/index.db" ] ||
		fail "Expected Python snapshot path helper to return the current DB archive path"
	[ "$(legacy_db_snapshot_archive_file)" = "$db_root/index.db.zst" ] ||
		fail "Expected Python snapshot path helper to return the legacy DB archive path"
	[ "$(legacy_sql_snapshot_archive_file)" = "$db_root/index.sql.zst" ] ||
		fail "Expected Python snapshot path helper to return the legacy SQL archive path"
	[ "$(db_snapshot_asset_name)" = "index.db" ] ||
		fail "Expected Python snapshot asset helper to return the current DB asset name"
	[ "$(legacy_db_snapshot_asset_name)" = "index.db.zst" ] ||
		fail "Expected Python snapshot asset helper to return the legacy DB asset name"
	[ "$(legacy_sql_snapshot_asset_name)" = "index.sql.zst" ] ||
		fail "Expected Python snapshot asset helper to return the legacy SQL asset name"
}

test_write_db_restore_signature_uses_python_snapshot_cli() {
	local db_root="$workdir/snapshot-helper-signature"
	local expected_signature

	mkdir -p "$db_root"
	BKG_INDEX_DB="$db_root/index.db"
	mkdir -p "$(dirname "$(db_snapshot_archive_file)")"
	printf '%s\n' 'snapshot-data' >"$(db_snapshot_archive_file)"

	write_db_restore_signature

	expected_signature=$(sha256sum "$(db_snapshot_archive_file)" | awk '{print $1}')
	[ "$(cat "$(db_restore_signature_file)")" = "$expected_signature" ] ||
		fail "Expected restore signature helper to write the current archive signature"
}

test_prepare_database_snapshot_uses_python_snapshot_cli() {
	local db_root="$workdir/snapshot-helper-prepare"
	local archive_file
	local expected_signature

	mkdir -p "$db_root"
	BKG_INDEX_DB="$db_root/index.db"
	BKG_INDEX_SQL="$db_root/index.sql"
	command sqlite3 "$BKG_INDEX_DB" "create table restored_payload (value text); insert into restored_payload (value) values ('prepared-db');"
	printf '%s\n' 'legacy-db' >"$(legacy_db_snapshot_archive_file)"
	printf '%s\n' 'legacy-sql' >"$(legacy_sql_snapshot_archive_file)"

	prepare_database_snapshot_for_archive

	archive_file=$(db_snapshot_archive_file)
	assert_file_exists "$archive_file"
	[ "$(command sqlite3 "$archive_file" "select value from restored_payload limit 1;")" = "prepared-db" ] ||
		fail "Expected Python snapshot prepare helper to publish the current database"
	[ ! -e "$(legacy_db_snapshot_archive_file)" ] ||
		fail "Expected Python snapshot prepare helper to remove legacy DB archives"
	[ ! -e "$(legacy_sql_snapshot_archive_file)" ] ||
		fail "Expected Python snapshot prepare helper to remove legacy SQL archives"
	[ ! -e "$archive_file.new" ] ||
		fail "Expected Python snapshot prepare helper to avoid Bash .new sidecars"
	expected_signature=$(sha256sum "$archive_file" | awk '{print $1}')
	[ "$(cat "$(db_restore_signature_file)")" = "$expected_signature" ] ||
		fail "Expected Python snapshot prepare helper to write the current restore signature"
}

test_rotate_database_snapshot_uses_python_storage() {
	local db_root="$workdir/snapshot-helper-rotate"
	local rotated_archive

	mkdir -p "$db_root"
	BKG_INDEX_DB="$db_root/index.db"
	sqlite_ensure_index_schema
	command sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_PKG' (owner_id, owner_type, package_type, owner, repo, package, downloads, downloads_month, downloads_week, downloads_day, size, date) values ('1','users','container','alpha','repo','pkg','1','1','1','1','1','2026-06-09'), ('1','users','container','alpha','repo','pkg','2','2','2','2','2','2026-06-10');"
	command sqlite3 "$BKG_INDEX_DB" "insert into '$BKG_INDEX_TBL_VER' (owner_id, owner_type, package_type, owner, repo, package, id, name, size, downloads, downloads_month, downloads_week, downloads_day, date, tags) values ('1','users','container','alpha','repo','pkg','old','sha256:old','1','1','1','1','1','2026-06-09','old'), ('1','users','container','alpha','repo','pkg','new','sha256:new','2','2','2','2','2','2026-06-10','latest');"
	mkdir -p "$(dirname "$(db_snapshot_archive_file)")"
	cp "$BKG_INDEX_DB" "$(db_snapshot_archive_file)"

	rotate_database_snapshot_if_needed 1 2026-06-10 2026.06.16

	rotated_archive="$(dirname "$(db_snapshot_archive_file)")/2026.06.16.index.db.zst"
	assert_file_exists "$rotated_archive"
	[ "$(command sqlite3 "$BKG_INDEX_DB" "select count(*) from packages where date < '2026-06-10';")" = "0" ] ||
		fail "Expected Python rotation helper to prune old package rows"
	[ "$(command sqlite3 "$BKG_INDEX_DB" "select count(*) from versions where date < '2026-06-10';")" = "0" ] ||
		fail "Expected Python rotation helper to prune old version rows"
	assert_file_exists "$(db_snapshot_archive_file)"
}

test_restore_db_from_snapshot_rebuilds_when_signature_changes() {
	local db_root="$workdir/db-restore-rebuild"
	local output_file="$db_root/output.txt"

	mkdir -p "$db_root"
	BKG_INDEX_DB="$db_root/index.db"
	mkdir -p "$(dirname "$(db_snapshot_archive_file)")"
	command sqlite3 "$(db_snapshot_archive_file)" "create table restored_payload (value text); insert into restored_payload (value) values ('snapshot-data-new');"
	command sqlite3 "$BKG_INDEX_DB" "create table restored_payload (value text); insert into restored_payload (value) values ('existing-db');"
	printf '%s\n' 'stale-signature' >"$(db_restore_signature_file)"

	restore_db_from_index_snapshot_if_needed >"$output_file"

	assert_contains "$output_file" "Restoring database from index.db"
	[ "$(command sqlite3 "$BKG_INDEX_DB" "select value from restored_payload limit 1;")" = "snapshot-data-new" ] ||
		fail "Expected current DB snapshot restore to replace the local database"
	[ "$(cat "$(db_restore_signature_file)")" = "$(current_index_snapshot_signature)" ] || fail "Expected restore to refresh the snapshot signature"
}

test_restore_db_from_legacy_compressed_snapshot() {
	local db_root="$workdir/db-restore-legacy-db"
	local output_file="$db_root/output.txt"
	local source_db="$db_root/source.db"

	mkdir -p "$db_root"
	BKG_INDEX_DB="$db_root/index.db"
	command sqlite3 "$source_db" "create table restored_payload (value text); insert into restored_payload (value) values ('snapshot-data-legacy-db');"
	zstd -q -f "$source_db" -o "$(legacy_db_snapshot_archive_file)"
	command sqlite3 "$BKG_INDEX_DB" "create table restored_payload (value text); insert into restored_payload (value) values ('existing-db');"
	printf '%s\n' 'stale-signature' >"$(db_restore_signature_file)"

	restore_db_from_index_snapshot_if_needed >"$output_file"

	assert_contains "$output_file" "Restoring database from index.db.zst"
	[ "$(command sqlite3 "$BKG_INDEX_DB" "select value from restored_payload limit 1;")" = "snapshot-data-legacy-db" ] ||
		fail "Expected legacy DB snapshot restore to replace the local database"
	[ "$(cat "$(db_restore_signature_file)")" = "$(current_index_snapshot_signature)" ] || fail "Expected legacy DB restore to refresh the snapshot signature"
}

test_restore_db_from_legacy_sql_snapshot() {
	local db_root="$workdir/db-restore-legacy"
	local output_file="$db_root/output.txt"
	local sql_file="$db_root/index.sql"

	mkdir -p "$db_root"
	BKG_INDEX_DB="$db_root/index.db"
	BKG_INDEX_SQL="$db_root/index.sql"
	cat >"$sql_file" <<'EOF'
create table restored_payload (value text);
insert into restored_payload (value) values ('legacy-snapshot-data');
EOF
	zstd -q -f "$sql_file" -o "$(legacy_sql_snapshot_archive_file)"

	restore_db_from_index_snapshot_if_needed >"$output_file"

	assert_contains "$output_file" "Restoring database from legacy index.sql.zst"
	[ "$(command sqlite3 "$BKG_INDEX_DB" "select value from restored_payload limit 1;")" = "legacy-snapshot-data" ] || fail "Expected legacy sql snapshot restore to import into sqlite"
}

test_corrupt_snapshot_restore_preserves_existing_database() {
	local db_root="$workdir/db-restore-corrupt"
	local output_file="$db_root/output.txt"
	local status=0

	mkdir -p "$db_root"
	BKG_INDEX_DB="$db_root/index.db"
	mkdir -p "$(dirname "$(db_snapshot_archive_file)")"
	printf '%s\n' 'not sqlite' >"$(db_snapshot_archive_file)"
	command sqlite3 "$BKG_INDEX_DB" "create table restored_payload (value text); insert into restored_payload (value) values ('existing-db');"

	if restore_db_from_index_snapshot_if_needed >"$output_file" 2>&1; then
		fail "Expected corrupt snapshot restore to fail"
	else
		status=$?
	fi

	[ "$status" -eq 1 ] || fail "Expected corrupt snapshot restore to return Python non-fatal status 1, got $status"
	[ "$(command sqlite3 "$BKG_INDEX_DB" "select value from restored_payload limit 1;")" = "existing-db" ] ||
		fail "Expected corrupt snapshot restore to preserve the existing database"
	[ ! -f "$(db_restore_signature_file)" ] || fail "Expected corrupt snapshot restore to avoid writing a restore signature"
}

test_update_startup_restores_snapshot_before_owner_count() {
	local db_root="$workdir/update-startup-restore"
	local output_file="$db_root/output.txt"
	local snapshot_file

	mkdir -p "$db_root"
	BKG_INDEX_DB="$db_root/index.db"
	mkdir -p "$(dirname "$(db_snapshot_archive_file)")"
	command sqlite3 "$(db_snapshot_archive_file)" "create table $BKG_INDEX_TBL_PKG (owner text); insert into $BKG_INDEX_TBL_PKG (owner) values ('alpha'), ('alpha'), ('beta');"

	snapshot_file=$(current_index_snapshot_archive_file)
	[ "$(index_database_owner_count)" = "0" ] ||
		fail "Expected owner count probe to tolerate a missing startup database"

	restore_startup_database_snapshot_if_needed "$snapshot_file" >"$output_file"

	assert_contains "$output_file" "Restoring database from index.db"
	[ "$(index_database_owner_count)" = "2" ] ||
		fail "Expected startup restore to make the downloaded snapshot available before owner count"
}

trap cleanup EXIT

source_project_script "bkg.sh"

run_test test_sqlite_retries_transient_write_failure
run_test test_sqlite_ensure_index_schema_adds_query_indexes
run_test test_batch_reset_uses_explicit_progress_only
run_test test_sqlite_numeric_version_ids_use_builtin_glob
run_test test_cleanup_generated_json_sidecars_removes_adaptive_retry_artifacts
run_test test_drop_replaced_legacy_version_tables_keeps_unreplaced_fallbacks
run_test test_parallel_async_wait_continues_after_non_timeout_failure
run_test test_parallel_async_default_max_jobs_is_tuned
run_test test_parallel_shell_func_preserves_inherited_runtime_config
run_test test_direct_bash_child_preserves_inherited_runtime_config
run_test test_util_default_env_path_is_absolute
run_test test_bkg_python_forwards_unexported_http_settings
run_test test_ensure_pages_dotfiles_visible_writes_nojekyll
run_test test_update_version_logs_sqlite_write_failure
run_test test_update_package_warns_on_package_level_fallback
run_test test_run_owner_page_discovery_stops_on_code_2
run_test test_run_owner_page_discovery_caps_at_one_page
run_test test_daily_gate_helpers_track_per_day
run_test test_daily_gate_skip_depends_on_master_commit_today
run_test test_daily_gate_skip_resets_on_new_batch_marker
run_test test_daily_gate_skip_resets_on_rest_to_top_change
run_test test_check_limit_retries_missing_script_start_once
run_test test_query_graphql_api_delegates_query_to_python
run_test test_query_api_delegates_path_to_python
run_test test_query_api_optional_allows_only_missing_responses
run_test test_dldb_delegates_release_snapshot_download_to_python
run_test test_dldb_check_mode_uses_python_release_metadata_probe
run_test test_resolve_release_snapshot_asset_rejects_http_errors
run_test test_check_db_deletes_missing_release_despite_stale_runtime_state
run_test test_check_db_reports_delete_failure
run_test test_resolve_owner_ids_uses_run_cache_before_live_lookup
run_test test_resolve_owner_ids_preserves_ids_and_records_graphql_misses
run_test test_resolve_owner_ids_falls_back_when_graphql_fails
run_test test_retire_missing_owner_removes_sparse_tree_and_manual_entry
run_test test_restore_db_from_snapshot_skips_when_signature_matches
run_test test_snapshot_helpers_use_python_archive_selection
run_test test_snapshot_path_helpers_use_python_derivation
run_test test_write_db_restore_signature_uses_python_snapshot_cli
run_test test_prepare_database_snapshot_uses_python_snapshot_cli
run_test test_rotate_database_snapshot_uses_python_storage
run_test test_restore_db_from_snapshot_rebuilds_when_signature_changes
run_test test_restore_db_from_legacy_compressed_snapshot
run_test test_restore_db_from_legacy_sql_snapshot
run_test test_corrupt_snapshot_restore_preserves_existing_database
run_test test_update_startup_restores_snapshot_before_owner_count

echo "Runtime regression tests passed"
