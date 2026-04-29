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

	curl_gh() {
		fail "Expected query_api to stop before calling curl_gh when the elapsed limit is exceeded"
	}

	if query_api "users/example" >/dev/null 2>&1; then
		fail "Expected query_api to return 3 when the elapsed limit is exceeded before the request starts"
	else
		status=$?
	fi

	[ "$status" -eq 3 ] || fail "Expected query_api to return 3 after elapsed limit preflight, got $status"
	unset -f curl_gh
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

	curl_gh() {
		fail "Expected query_graphql_api to stop before calling curl_gh when the elapsed limit is exceeded"
	}

	if query_graphql_api 'query { viewer { login } }' >/dev/null 2>&1; then
		fail "Expected query_graphql_api to return 3 when the elapsed limit is exceeded before the request starts"
	else
		status=$?
	fi

	[ "$status" -eq 3 ] || fail "Expected query_graphql_api to return 3 after elapsed limit preflight, got $status"
	unset -f curl_gh
	GITHUB_TOKEN=""
}

trap cleanup EXIT

source_project_script "bkg.sh"

run_test test_parallel_shell_func_timeout_fallback
run_test test_curl_stops_retrying_after_timeout
run_test test_curl_checks_elapsed_limit_before_successful_request
run_test test_docker_manifest_inspect_stops_after_timeout
run_test test_ytoxt_stops_after_timeout
run_test test_run_parallel_kills_blocked_workers_after_timeout
run_test test_parallel_async_wait_kills_blocked_workers_after_timeout
run_test test_owner_update_wait_notice_is_throttled
run_test test_owner_update_force_stop_due_after_grace_period
run_test test_run_owner_updates_halts_on_timeout
run_test test_query_api_checks_elapsed_limit_before_request
run_test test_query_graphql_api_checks_elapsed_limit_before_request

echo "Timeout propagation regression tests passed"