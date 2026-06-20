#!/bin/bash

# shellcheck disable=SC2034

test_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
src_dir=$(cd "$test_dir/.." && pwd)
workdir=$(mktemp -d)

cleanup() {
	rm -rf "$workdir"
}

fail() {
	echo "$1" >&2
	exit 1
}

assert_file_exists() {
	[ -f "$1" ] || fail "Expected file to exist: $1"
}

assert_contains() {
	grep -Fq -- "$2" "$1" || fail "Expected $1 to contain $2"
}

assert_not_contains() {
	! grep -Fq -- "$2" "$1" || fail "Expected $1 to not contain $2"
}

run_test() {
	[ -n "${1:-}" ] || fail "Expected a test function name"
	local started_at=0
	local status

	if [ "${BKG_TEST_TIMINGS:-0}" = "1" ]; then
		started_at=$(date +%s%3N)
	fi

	if ("$1"); then
		if ((started_at > 0)); then
			printf 'Test timing: %s %dms\n' "$1" "$(( $(date +%s%3N) - started_at ))"
		fi
		return 0
	else
		status=$?
	fi

	if ((started_at > 0)); then
		printf 'Test timing: %s %dms\n' "$1" "$(( $(date +%s%3N) - started_at ))" >&2
	fi
	echo "Test failed: $1" >&2
	return "$status"
}

source_project_script() {
	[ -n "${1:-}" ] || fail "Expected a project script path"

	pushd "$src_dir" >/dev/null || return 1
	export BKG_SKIP_DEP_VERIFY=1
	# shellcheck disable=SC1090
	source "$1"
	popd >/dev/null || return 1
}

init_bkg_runtime_state() {
	[ -n "${1:-}" ] || fail "Expected an env file path"
	local env_file=$1
	local now=${2:-$(date -u +%s)}

	BKG_ENV="$env_file"
	: >"$BKG_ENV"
	BKG_SCRIPT_START="$now"
	set_BKG BKG_SCRIPT_START "$now"
	set_BKG BKG_RATE_LIMIT_START "$now"
	set_BKG BKG_MIN_RATE_LIMIT_START "$now"
	set_BKG BKG_CALLS_TO_API 0
	set_BKG BKG_MIN_CALLS_TO_API 0
}
