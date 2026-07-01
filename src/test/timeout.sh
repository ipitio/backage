#!/bin/bash

# Test doubles are invoked indirectly by sourced production functions.
# shellcheck disable=SC1091,SC2034,SC2317

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
	echo 'jq: parse error: Invalid string: control characters from U+0000 through U+001F must be escaped' >&2
	echo 'GitHub operation exceeded its total timeout' >&2
	echo 'Unable to derive container size from alpha/pkg/1 embedded manifest: malformed JSON; sample="{\"bad\":\"raw \\u0001 control\"}"' >&2
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
	assert_not_contains "$output_file" "jq: parse error:"
	assert_not_contains "$output_file" "GitHub operation exceeded its total timeout"
}

test_parallel_shell_func_timeout_stderr_filter_keeps_manifest_diagnostics() {
	local stderr_file="$workdir/timeout-stderr-filter.err"
	local output_file="$workdir/timeout-stderr-filter.out"

	{
		echo 'parallel: This job failed:'
		echo 'bash /tmp/parallel-worker.sh /tmp/source worker'
		echo 'parallel: Starting no more jobs. Waiting for 2 jobs to finish.'
		echo 'jq: parse error: Invalid string: control characters from U+0000 through U+001F must be escaped'
		echo 'GitHub operation exceeded its total timeout'
		printf '%s\n' 'Unable to derive container size from alpha/pkg/1 embedded manifest: malformed JSON; sample="{\"bad\":\"raw \\u0001 control\"}"'
	} >"$stderr_file"

	parallel_shell_func_print_timeout_stderr "$stderr_file" >"$output_file" 2>&1

	assert_not_contains "$output_file" "parallel: This job failed:"
	assert_not_contains "$output_file" "parallel: Starting no more jobs."
	assert_not_contains "$output_file" "jq: parse error:"
	assert_not_contains "$output_file" "GitHub operation exceeded its total timeout"
	assert_contains "$output_file" "Unable to derive container size from alpha/pkg/1 embedded manifest: malformed JSON"
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

test_curl_checks_elapsed_limit_before_successful_request() {
	local fake_bin="$workdir/fake-curl-preflight-bin"
	local fake_curl="$fake_bin/curl"
	local attempts_file="$workdir/curl-preflight-attempts.txt"
	local original_path="$PATH"
	local status=0
	local now

	mkdir -p "$fake_bin"
	cat >"$fake_curl" <<'EOF'
#!/bin/bash
printf '1\n' >"$TEST_CURL_PREFLIGHT_ATTEMPTS_FILE"
printf 'ok\n'
EOF
	chmod +x "$fake_curl"

	now=$(date -u +%s)
	BKG_ENV="$workdir/env-curl-preflight.env"
	: >"$BKG_ENV"
	set_BKG BKG_SCRIPT_START "$((now - 5))"
	set_BKG BKG_RATE_LIMIT_START "$now"
	set_BKG BKG_MIN_RATE_LIMIT_START "$now"
	set_BKG BKG_CALLS_TO_API 0
	set_BKG BKG_MIN_CALLS_TO_API 0
	BKG_MAX_LEN=1
	TEST_CURL_PREFLIGHT_ATTEMPTS_FILE="$attempts_file"
	export TEST_CURL_PREFLIGHT_ATTEMPTS_FILE BKG_ENV
	PATH="$fake_bin:$original_path"

	if curl "https://example.invalid" >/dev/null 2>&1; then
		fail "Expected curl wrapper to stop before a successful request when the elapsed limit is exceeded"
	else
		status=$?
	fi

	PATH="$original_path"
	[ "$status" -eq 3 ] || fail "Expected curl wrapper to return 3 after elapsed limit preflight, got $status"
	[ ! -f "$attempts_file" ] || fail "Expected curl wrapper to avoid starting curl after elapsed limit preflight"
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

test_run_parallel_enforces_elapsed_limit_for_blocked_workers() {
	local started_file="$workdir/run-parallel-elapsed-started.txt"
	local status=0
	local started_at
	local elapsed
	local now

	blocking_parallel_worker_without_timeout() {
		printf '%s\n' "$1" >>"$started_file"
		exec sleep 30
	}

	now=$(date -u +%s)
	BKG_ENV="$workdir/env-run-parallel-elapsed.env"
	: >"$BKG_ENV"
	set_BKG BKG_SCRIPT_START "$now"
	set_BKG BKG_RATE_LIMIT_START "$now"
	set_BKG BKG_MIN_RATE_LIMIT_START "$now"
	set_BKG BKG_CALLS_TO_API 0
	set_BKG BKG_MIN_CALLS_TO_API 0
	set_BKG BKG_TIMEOUT 0
	BKG_MAX_LEN=3
	: >"$started_file"
	started_at=$(date +%s)

	if run_parallel blocking_parallel_worker_without_timeout $'one\ntwo'; then
		fail "Expected run_parallel to enforce the elapsed timeout"
	else
		status=$?
	fi

	elapsed=$(( $(date +%s) - started_at ))
	[ "$status" -eq 3 ] || fail "Expected run_parallel to return 3 after elapsed timeout, got $status"
	assert_contains "$started_file" "one"
	[ "$elapsed" -lt 10 ] || fail "Expected run_parallel to terminate blocked workers after elapsed timeout"
	unset -f blocking_parallel_worker_without_timeout
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

test_run_owner_updates_halts_on_timeout() {
	local args_file="$workdir/owner-update.args"
	local started_at
	local elapsed
	local status=0

	get_BKG_set() {
		printf '1/alpha\n2/beta\n'
	}

	current_batch_first_started() {
		printf '%s\n' 2026-07-01
	}
	get_BKG() {
		[ "$1" = BKG_BATCH_MARKER ] && printf '%s\n' batch-1
	}
	bkg_python() {
		printf '%s\n' "$*" >"$args_file"
		return 3
	}

	fast_out=false
	started_at=$(date +%s)

	if run_owner_updates; then
		fail "Expected run_owner_updates to return 3 when owner workers time out"
	else
		status=$?
	fi
	elapsed=$(( $(date +%s) - started_at ))

	[ "$status" -eq 3 ] || fail "Expected run_owner_updates to return 3, got $status"
	[ "$elapsed" -lt 10 ] || fail "Expected run_owner_updates to notice completed workers promptly"
	assert_contains "$args_file" "orchestration update-owners 2026-07-01 batch-1 false"
	unset -f current_batch_first_started
	unset -f get_BKG
	unset -f bkg_python
}

test_owner_update_status_keeps_graceful_timeout_publishable() {
	local output_file="$workdir/owner-timeout-status.out"
	local status=0

	bkg_python() {
		[ "$*" = "orchestration owner-phase-decision 3 0" ] || fail "Unexpected owner phase decision arguments: $*"
		printf 'publish\t3\tReached BKG_MAX_LEN, stopping after persisting state...\n'
	}
	return_code=0
	handle_owner_update_status 3 >"$output_file" 2>&1 || status=$?

	[ "$status" -eq 0 ] || fail "Expected graceful owner timeout to keep publishing path available"
	[ "$return_code" -eq 3 ] || fail "Expected graceful owner timeout to persist return_code 3"
	assert_contains "$output_file" "Reached BKG_MAX_LEN"
	unset -f bkg_python
}

test_owner_update_status_aborts_unexpected_failure() {
	local output_file="$workdir/owner-failure-status.out"
	local status=0

	bkg_python() {
		[ "$*" = "orchestration owner-phase-decision 1 0" ] || fail "Unexpected owner phase decision arguments: $*"
		printf 'abort\t1\tOwner updates failed with status 1; stopping before snapshot publication.\n'
	}
	return_code=0
	handle_owner_update_status 1 >"$output_file" 2>&1 || status=$?

	[ "$status" -eq 1 ] || fail "Expected unexpected owner failure to abort with status 1"
	[ "$return_code" -eq 0 ] || fail "Expected unexpected owner failure not to mark graceful timeout"
	assert_contains "$output_file" "stopping before snapshot publication"
	unset -f bkg_python
}

test_query_api_checks_elapsed_limit_before_request() {
	local status=0
	local now

	now=$(date -u +%s)
	BKG_ENV="$workdir/env-query-api-preflight.env"
	: >"$BKG_ENV"
	set_BKG BKG_SCRIPT_START "$((now - 5))"
	set_BKG BKG_RATE_LIMIT_START "$now"
	set_BKG BKG_MIN_RATE_LIMIT_START "$now"
	set_BKG BKG_CALLS_TO_API 0
	set_BKG BKG_MIN_CALLS_TO_API 0
	BKG_MAX_LEN=1
	GITHUB_TOKEN=dummy

	bkg_python() {
		fail "Expected query_api to stop before calling Python when the elapsed limit is exceeded"
	}

	if query_api "users/example" >/dev/null 2>&1; then
		fail "Expected query_api to return 3 when the elapsed limit is exceeded before the request starts"
	else
		status=$?
	fi

	[ "$status" -eq 3 ] || fail "Expected query_api to return 3 after elapsed limit preflight, got $status"
	unset -f bkg_python
	GITHUB_TOKEN=""
}

test_query_graphql_api_checks_elapsed_limit_before_request() {
	local status=0
	local now

	now=$(date -u +%s)
	BKG_ENV="$workdir/env-query-graphql-preflight.env"
	: >"$BKG_ENV"
	set_BKG BKG_SCRIPT_START "$((now - 5))"
	set_BKG BKG_RATE_LIMIT_START "$now"
	set_BKG BKG_MIN_RATE_LIMIT_START "$now"
	set_BKG BKG_CALLS_TO_API 0
	set_BKG BKG_MIN_CALLS_TO_API 0
	BKG_MAX_LEN=1
	GITHUB_TOKEN=dummy

	bkg_python() {
		fail "Expected query_graphql_api to stop before calling Python when the elapsed limit is exceeded"
	}

	if query_graphql_api 'query { viewer { login } }' >/dev/null 2>&1; then
		fail "Expected query_graphql_api to return 3 when the elapsed limit is exceeded before the request starts"
	else
		status=$?
	fi

	[ "$status" -eq 3 ] || fail "Expected query_graphql_api to return 3 after elapsed limit preflight, got $status"
	unset -f bkg_python
	GITHUB_TOKEN=""
}

test_page_owner_checks_elapsed_limit_before_request() {
	local status=0
	local now

	now=$(date -u +%s)
	BKG_ENV="$workdir/env-page-owner-preflight.env"
	: >"$BKG_ENV"
	set_BKG BKG_SCRIPT_START "$((now - 5))"
	set_BKG BKG_RATE_LIMIT_START "$now"
	set_BKG BKG_MIN_RATE_LIMIT_START "$now"
	set_BKG BKG_CALLS_TO_API 0
	set_BKG BKG_MIN_CALLS_TO_API 0
	BKG_MAX_LEN=1
	BKG_PAGE_ALL=1
	GITHUB_TOKEN=dummy

	bkg_python() {
		fail "Expected page_owner to stop before calling Python when the elapsed limit is exceeded"
	}

	if page_owner 1 >/dev/null 2>&1; then
		fail "Expected page_owner to return 3 when the elapsed limit is exceeded before the request starts"
	else
		status=$?
	fi

	[ "$status" -eq 3 ] || fail "Expected page_owner to return 3 after elapsed limit preflight, got $status"
	unset -f bkg_python
	GITHUB_TOKEN=""
}

trap cleanup EXIT

source_project_script "bkg.sh"

run_test test_parallel_shell_func_timeout_fallback
run_test test_parallel_shell_func_timeout_stderr_filter_keeps_manifest_diagnostics
run_test test_curl_stops_retrying_after_timeout
run_test test_curl_checks_elapsed_limit_before_successful_request
run_test test_ytoxt_stops_after_timeout
run_test test_run_parallel_kills_blocked_workers_after_timeout
run_test test_run_parallel_enforces_elapsed_limit_for_blocked_workers
run_test test_parallel_async_wait_kills_blocked_workers_after_timeout
run_test test_run_owner_updates_halts_on_timeout
run_test test_owner_update_status_keeps_graceful_timeout_publishable
run_test test_owner_update_status_aborts_unexpected_failure
run_test test_query_api_checks_elapsed_limit_before_request
run_test test_query_graphql_api_checks_elapsed_limit_before_request
run_test test_page_owner_checks_elapsed_limit_before_request

echo "Timeout propagation regression tests passed"
