#!/bin/bash

# shellcheck disable=SC1091,SC2034

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

test_update_version_logs_sqlite_write_failure() {
	local row
	local fake_bin="$workdir/fake-sqlite-flush-bin"
	local fake_sqlite="$fake_bin/sqlite3"
	local original_path="$PATH"
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
		mkdir -p "$fake_bin"
		cat >"$fake_sqlite" <<'EOF'
#!/bin/bash
exit 1
EOF
		chmod +x "$fake_sqlite"
		curl() {
			cat <<'EOF'
<span>Total downloads</span><span>984</span><span>Last 30 days</span><span>984</span><span>Last week</span><span>454</span><span>Today</span><span>2</span><pre><code>{"schemaVersion":2,"layers":[{"size":123}]}</code></pre>
EOF
		}
		docker_manifest_inspect() {
			printf '%s' '{"schemaVersion":2,"layers":[{"size":123}]}'
		}
		update_version "$row"
		PATH="$fake_bin:$original_path"
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

test_query_graphql_api_tracks_cost_and_remaining() {
	BKG_ENV="$workdir/env-graphql.env"
	: >"$BKG_ENV"
	set_BKG BKG_CALLS_TO_API 5
	set_BKG BKG_MIN_CALLS_TO_API 7
	GITHUB_TOKEN=dummy

	curl_gh() {
		[ "$1" = "-X" ] || fail "Expected query_graphql_api to call curl_gh with -X POST"
		[ "$2" = "POST" ] || fail "Expected query_graphql_api to use POST"
		assert_contains <(printf '%s\n' "$5") 'rateLimit { cost remaining resetAt }'
		cat <<'EOF'
{"data":{"viewer":{"login":"ipitio"},"rateLimit":{"cost":17,"remaining":4321,"resetAt":"2026-04-10T23:59:59Z"}}}
EOF
	}

	query_graphql_api 'query { viewer { login } }' >/tmp/query-graphql.out

	[ "$(get_BKG BKG_CALLS_TO_API)" = "22" ] || fail "Expected query_graphql_api to add GraphQL cost to BKG_CALLS_TO_API"
	[ "$(get_BKG BKG_MIN_CALLS_TO_API)" = "24" ] || fail "Expected query_graphql_api to add GraphQL cost to BKG_MIN_CALLS_TO_API"
	[ "$(get_BKG BKG_GRAPHQL_LAST_COST)" = "17" ] || fail "Expected query_graphql_api to persist the last GraphQL cost"
	[ "$(get_BKG BKG_GRAPHQL_REMAINING)" = "4321" ] || fail "Expected query_graphql_api to persist GraphQL remaining budget"
	[ "$(get_BKG BKG_GRAPHQL_RESET_AT)" = "2026-04-10T23:59:59Z" ] || fail "Expected query_graphql_api to persist the GraphQL reset time"
	unset -f curl_gh
	GITHUB_TOKEN=""
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

test_resolve_owner_ids_preserves_ids_and_batches_live_lookup() {
	local candidates_file="$workdir/owner-candidates.txt"
	local output_file="$workdir/owner-ids.txt"
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
{"data":{"o0":{"login":"beta","databaseId":200},"o1":{"login":"gamma","databaseId":300},"o2":null}}
EOF
	}

	owner_get_id() {
		[ "$1" = "delta" ] || fail "Expected only unresolved delta to fall back to owner_get_id"
		printf '%s\n' '400/delta'
	}

	resolve_owner_ids "$candidates_file" >"$output_file"

	assert_contains "$output_file" "123/alpha"
	assert_contains "$output_file" "200/beta"
	assert_contains "$output_file" "300/gamma"
	assert_contains "$output_file" "400/delta"
	assert_contains "$query_log" 'repositoryOwner(login:"beta")'
	assert_contains "$query_log" 'repositoryOwner(login:"gamma")'
	assert_contains "$query_log" 'repositoryOwner(login:"delta")'
	unset -f query_graphql_api
	unset -f owner_get_id
	GITHUB_TOKEN=""
}

test_restore_db_from_snapshot_skips_when_signature_matches() {
	local db_root="$workdir/db-restore-skip"
	local output_file="$db_root/output.txt"

	mkdir -p "$db_root"
	BKG_INDEX_DB="$db_root/index.db"
	printf '%s\n' 'snapshot-data' >"$(db_snapshot_archive_file)"
	printf '%s\n' 'existing-db' >"$BKG_INDEX_DB"
	current_index_snapshot_signature >"$(db_restore_signature_file)"

	unzstd() {
		fail "Expected matching snapshot signature to skip database restore"
	}

	restore_db_from_index_snapshot_if_needed >"$output_file"

	unset -f unzstd
	assert_contains "$output_file" "Using existing database; index.db.zst unchanged"
	assert_contains "$BKG_INDEX_DB" "existing-db"
}

test_restore_db_from_snapshot_rebuilds_when_signature_changes() {
	local db_root="$workdir/db-restore-rebuild"
	local output_file="$db_root/output.txt"

	mkdir -p "$db_root"
	BKG_INDEX_DB="$db_root/index.db"
	printf '%s\n' 'snapshot-data-new' >"$(db_snapshot_archive_file)"
	printf '%s\n' 'existing-db' >"$BKG_INDEX_DB"
	printf '%s\n' 'stale-signature' >"$(db_restore_signature_file)"

	unzstd() {
		[ "${1:-}" = "-c" ] || fail "Expected restore to invoke unzstd with -c"
		cat "$2"
	}

	restore_db_from_index_snapshot_if_needed >"$output_file"

	unset -f unzstd
	assert_contains "$output_file" "Restoring database from index.db.zst"
	assert_contains "$BKG_INDEX_DB" "snapshot-data-new"
	[ "$(cat "$(db_restore_signature_file)")" = "$(current_index_snapshot_signature)" ] || fail "Expected restore to refresh the snapshot signature"
}

test_restore_db_from_legacy_sql_snapshot() {
	local db_root="$workdir/db-restore-legacy"
	local output_file="$db_root/output.txt"

	mkdir -p "$db_root"
	BKG_INDEX_DB="$db_root/index.db"
	BKG_INDEX_SQL="$db_root/index.sql"
	cat >"$(legacy_sql_snapshot_archive_file)" <<'EOF'
create table restored_payload (value text);
insert into restored_payload (value) values ('legacy-snapshot-data');
EOF

	unzstd() {
		[ "${1:-}" = "-c" ] || fail "Expected legacy restore to invoke unzstd with -c"
		cat "$2"
	}

	restore_db_from_index_snapshot_if_needed >"$output_file"

	unset -f unzstd
	assert_contains "$output_file" "Restoring database from legacy index.sql.zst"
	[ "$(command sqlite3 "$BKG_INDEX_DB" "select value from restored_payload limit 1;")" = "legacy-snapshot-data" ] || fail "Expected legacy sql snapshot restore to import into sqlite"
}

trap cleanup EXIT

source_project_script "bkg.sh"

run_test test_sqlite_retries_transient_write_failure
run_test test_parallel_async_wait_continues_after_non_timeout_failure
run_test test_parallel_async_default_max_jobs_is_tuned
run_test test_update_version_logs_sqlite_write_failure
run_test test_update_package_warns_on_package_level_fallback
run_test test_run_owner_page_discovery_stops_on_code_2
run_test test_run_owner_page_discovery_caps_at_one_page
run_test test_daily_gate_helpers_track_per_day
run_test test_daily_gate_skip_depends_on_master_commit_today
run_test test_daily_gate_skip_resets_on_new_batch_marker
run_test test_daily_gate_skip_resets_on_rest_to_top_change
run_test test_check_limit_retries_missing_script_start_once
run_test test_query_graphql_api_tracks_cost_and_remaining
run_test test_resolve_owner_ids_uses_run_cache_before_live_lookup
run_test test_resolve_owner_ids_preserves_ids_and_batches_live_lookup
run_test test_restore_db_from_snapshot_skips_when_signature_matches
run_test test_restore_db_from_snapshot_rebuilds_when_signature_changes
run_test test_restore_db_from_legacy_sql_snapshot

echo "Runtime regression tests passed"