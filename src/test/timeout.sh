#!/bin/bash

# shellcheck disable=SC1091,SC2034

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
src_dir=${src_dir:?}
workdir=${workdir:?}

test_parallel_shell_func_timeout_fallback() {
	local fixture_file="$workdir/timeout-worker.sh"
	local input_file="$workdir/timeout-input.txt"
	local output_file="$workdir/timeout-output.txt"
	local status=0

	cat >"$fixture_file" <<'EOF'
#!/bin/bash

timeout_worker() {
	return 3
}
EOF

	printf 'one\ntwo\n' >"$input_file"
	BKG_ENV="$workdir/env-timeout.env"
	: >"$BKG_ENV"
	set_BKG BKG_TIMEOUT "1"

	if parallel_shell_func "$fixture_file" timeout_worker --lb <"$input_file" >"$output_file" 2>&1; then
		fail "Expected parallel_shell_func to surface timeout status 3"
	else
		status=$?
	fi

	[ "$status" -eq 3 ] || fail "Expected parallel_shell_func to return 3 after timeout, got $status"
	assert_not_contains "$output_file" "parallel: This job failed:"
	assert_not_contains "$output_file" "parallel: Starting no more jobs."
}

test_curl_stops_retrying_after_timeout() {
	local fake_bin="$workdir/fake-bin"
	local fake_curl="$fake_bin/curl"
	local attempts_file="$workdir/curl-attempts.txt"
	local started_file="$workdir/curl-started.txt"
	local original_path="$PATH"
	local started_at
	local elapsed
	local status=0

	mkdir -p "$fake_bin"
	cat >"$fake_curl" <<'EOF'
#!/bin/bash

attempts=0
[ ! -f "$TEST_CURL_ATTEMPTS_FILE" ] || attempts=$(cat "$TEST_CURL_ATTEMPTS_FILE")
attempts=$((attempts + 1))
printf '%s\n' "$attempts" >"$TEST_CURL_ATTEMPTS_FILE"
printf 'started\n' >"$TEST_CURL_STARTED_FILE"

if [ "$attempts" -eq 1 ]; then
	printf 'BKG_TIMEOUT=1\n' >"$BKG_ENV"
	exec sleep 30
fi

exit 1
EOF
	chmod +x "$fake_curl"

	BKG_ENV="$workdir/env-curl.env"
	: >"$BKG_ENV"
	TEST_CURL_ATTEMPTS_FILE="$attempts_file"
	TEST_CURL_STARTED_FILE="$started_file"
	export TEST_CURL_ATTEMPTS_FILE TEST_CURL_STARTED_FILE BKG_ENV
	PATH="$fake_bin:$original_path"
	started_at=$(date +%s)

	if curl "https://example.invalid" >/dev/null 2>&1; then
		fail "Expected curl wrapper to stop with status 3 after timeout"
	else
		status=$?
	fi

	PATH="$original_path"
	elapsed=$(( $(date +%s) - started_at ))
	[ "$status" -eq 3 ] || fail "Expected curl wrapper to return 3 after timeout, got $status"
	[ "$(cat "$attempts_file")" -eq 1 ] || fail "Expected curl wrapper to stop retrying after timeout"
	assert_file_exists "$started_file"
	[ "$elapsed" -lt 10 ] || fail "Expected curl wrapper to interrupt a running request promptly"
}

test_docker_manifest_inspect_stops_after_timeout() {
	local fake_bin="$workdir/fake-docker-bin"
	local fake_docker="$fake_bin/docker"
	local started_file="$workdir/docker-started.txt"
	local original_path="$PATH"
	local started_at
	local elapsed
	local status=0

	mkdir -p "$fake_bin"
	cat >"$fake_docker" <<'EOF'
#!/bin/bash
printf 'started\n' >"$TEST_DOCKER_STARTED_FILE"
printf 'BKG_TIMEOUT=1\n' >"$BKG_ENV"
exec sleep 30
EOF
	chmod +x "$fake_docker"

	BKG_ENV="$workdir/env-docker.env"
	: >"$BKG_ENV"
	TEST_DOCKER_STARTED_FILE="$started_file"
	export TEST_DOCKER_STARTED_FILE BKG_ENV
	PATH="$fake_bin:$original_path"
	started_at=$(date +%s)

	if docker_manifest_inspect "ghcr.io/example/pkg:latest" >/dev/null 2>&1; then
		fail "Expected docker_manifest_inspect to stop with status 3 after timeout"
	else
		status=$?
	fi

	PATH="$original_path"
	elapsed=$(( $(date +%s) - started_at ))
	[ "$status" -eq 3 ] || fail "Expected docker_manifest_inspect to return 3 after timeout, got $status"
	assert_file_exists "$started_file"
	[ "$elapsed" -lt 10 ] || fail "Expected docker_manifest_inspect to interrupt promptly after timeout"
}

test_ytoxt_stops_after_timeout() {
	local json_file="$workdir/ytoxt-timeout.json"
	local status=0

	printf '%s\n' '{"package":[]}' >"$json_file"
	BKG_ENV="$workdir/env-ytoxt.env"
	printf 'BKG_TIMEOUT=1\n' >"$BKG_ENV"

	if bash "$src_dir/lib/ytoxt.sh" "$json_file" >/dev/null 2>&1; then
		fail "Expected ytoxt.sh to return 3 when timeout is already requested"
	else
		status=$?
	fi

	[ "$status" -eq 3 ] || fail "Expected ytoxt.sh to return 3 after timeout, got $status"
}

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

test_run_parallel_kills_blocked_workers_after_timeout() {
	local started_file="$workdir/run-parallel-started.txt"
	local status=0
	local started_at
	local elapsed

	blocking_parallel_worker() {
		printf '%s\n' "$1" >>"$started_file"
		printf 'BKG_TIMEOUT=1\n' >"$BKG_ENV"
		exec sleep 30
	}

	BKG_ENV="$workdir/env-run-parallel.env"
	: >"$BKG_ENV"
	: >"$started_file"
	started_at=$(date +%s)

	if run_parallel blocking_parallel_worker $'one\ntwo'; then
		fail "Expected run_parallel to return 3 after timeout"
	else
		status=$?
	fi

	elapsed=$(( $(date +%s) - started_at ))
	[ "$status" -eq 3 ] || fail "Expected run_parallel to return 3 after timeout, got $status"
	assert_contains "$started_file" "one"
	[ "$elapsed" -lt 10 ] || fail "Expected run_parallel to terminate blocked workers promptly"
	unset -f blocking_parallel_worker
}

test_parallel_async_wait_kills_blocked_workers_after_timeout() {
	local started_file="$workdir/parallel-async-started.txt"
	local status=0
	local started_at
	local elapsed

	blocking_async_worker() {
		printf '%s\n' "$1" >>"$started_file"
		printf 'BKG_TIMEOUT=1\n' >"$BKG_ENV"
		exec sleep 30
	}

	BKG_ENV="$workdir/env-parallel-async.env"
	: >"$BKG_ENV"
	: >"$started_file"
	started_at=$(date +%s)

	parallel_async_submit blocking_async_worker "one"
	parallel_async_wait || status=$?

	elapsed=$(( $(date +%s) - started_at ))
	[ "$status" -eq 3 ] || fail "Expected parallel_async_wait to return 3 after timeout, got $status"
	assert_contains "$started_file" "one"
	[ "$elapsed" -lt 10 ] || fail "Expected parallel_async_wait to terminate blocked workers promptly"
	unset -f blocking_async_worker
}

test_owner_update_wait_notice_is_throttled() {
	local started_at=0
	local last_notice_at=0
	local original_message
	local followup_message
	local throttled_message

	date() {
		if [ "$1" = "-u" ] && [ "$2" = "+%s" ]; then
			printf '%s\n' "${TEST_FAKE_NOW:-0}"
			return 0
		fi

		command date "$@"
	}

	TEST_FAKE_NOW=100
	owner_update_wait_notice "$started_at" "$last_notice_at"
	started_at=$OWNER_UPDATE_WAIT_STARTED
	last_notice_at=$OWNER_UPDATE_WAIT_LAST_NOTICE
	original_message=$OWNER_UPDATE_WAIT_MESSAGE

	TEST_FAKE_NOW=130
	owner_update_wait_notice "$started_at" "$last_notice_at"
	started_at=$OWNER_UPDATE_WAIT_STARTED
	last_notice_at=$OWNER_UPDATE_WAIT_LAST_NOTICE
	throttled_message=$OWNER_UPDATE_WAIT_MESSAGE

	TEST_FAKE_NOW=401
	owner_update_wait_notice "$started_at" "$last_notice_at"
	followup_message=$OWNER_UPDATE_WAIT_MESSAGE

	[ "$original_message" = "Waiting for active owner updates to stop..." ] || fail "Expected initial wait message"
	[ -z "$throttled_message" ] || fail "Expected wait message to be throttled before interval elapses"
	[ "$followup_message" = "Still waiting for active owner updates to stop after 301s..." ] || fail "Expected throttled follow-up wait message"
	unset TEST_FAKE_NOW
	unset -f date
}

test_owner_update_force_stop_due_after_grace_period() {
	date() {
		if [ "$1" = "-u" ] && [ "$2" = "+%s" ]; then
			printf '%s\n' "${TEST_FAKE_NOW:-0}"
			return 0
		fi

		command date "$@"
	}

	TEST_FAKE_NOW=250
	owner_update_force_stop_due 100 180
	[ "$OWNER_UPDATE_FORCE_STOP_DUE" = "false" ] || fail "Expected force stop to remain disabled before grace period"

	TEST_FAKE_NOW=280
	owner_update_force_stop_due 100 180
	[ "$OWNER_UPDATE_FORCE_STOP_DUE" = "true" ] || fail "Expected force stop to activate after grace period"

	unset TEST_FAKE_NOW
	unset -f date
}

test_run_owner_updates_halts_on_timeout() {
	local args_file="$workdir/owner-update.args"
	local stdin_file="$workdir/owner-update.stdin"
	local status=0

	get_BKG_set() {
		printf '1/alpha\n2/beta\n'
	}

	git() {
		if [ "$1" = "branch" ] && [ "$2" = "--show-current" ]; then
			echo master
			return 0
		fi

		command git "$@"
	}

	parallel_shell_func() {
		printf '%s\n' "$@" >"$args_file"
		cat >"$stdin_file"
		return 3
	}

	GITHUB_OWNER=ipitio

	if run_owner_updates; then
		fail "Expected run_owner_updates to return 3 when owner workers time out"
	else
		status=$?
	fi

	[ "$status" -eq 3 ] || fail "Expected run_owner_updates to return 3, got $status"
	assert_contains "$args_file" "update_owner"
	assert_contains "$args_file" "--halt"
	assert_contains "$args_file" "soon,fail=1"
	assert_contains "$stdin_file" "1/alpha"
	assert_contains "$stdin_file" "2/beta"
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

trap cleanup EXIT

pushd "$src_dir" >/dev/null
export BKG_SKIP_DEP_VERIFY=1
source bkg.sh
popd >/dev/null

test_parallel_shell_func_timeout_fallback
test_curl_stops_retrying_after_timeout
test_docker_manifest_inspect_stops_after_timeout
test_ytoxt_stops_after_timeout
test_sqlite_retries_transient_write_failure
test_parallel_async_wait_continues_after_non_timeout_failure
test_parallel_async_default_max_jobs_is_tuned
test_update_version_logs_sqlite_write_failure
test_update_package_warns_on_package_level_fallback
test_run_parallel_kills_blocked_workers_after_timeout
test_parallel_async_wait_kills_blocked_workers_after_timeout
test_owner_update_wait_notice_is_throttled
test_owner_update_force_stop_due_after_grace_period
test_run_owner_updates_halts_on_timeout
test_run_owner_page_discovery_stops_on_code_2
test_run_owner_page_discovery_caps_at_one_page
test_query_graphql_api_tracks_cost_and_remaining
test_resolve_owner_ids_preserves_ids_and_batches_live_lookup
test_restore_db_from_snapshot_skips_when_signature_matches
test_restore_db_from_snapshot_rebuilds_when_signature_changes
test_restore_db_from_legacy_sql_snapshot

echo "Timeout propagation regression tests passed"