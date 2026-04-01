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

trap cleanup EXIT

pushd "$src_dir" >/dev/null
export BKG_SKIP_DEP_VERIFY=1
source bkg.sh
popd >/dev/null

test_parallel_shell_func_timeout_fallback
test_curl_stops_retrying_after_timeout
test_docker_manifest_inspect_stops_after_timeout
test_ytoxt_stops_after_timeout
test_run_owner_updates_halts_on_timeout

echo "Timeout propagation regression tests passed"