#!/bin/bash

# shellcheck disable=SC1091,SC2034

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
src_dir=${src_dir:?}
workdir=${workdir:?}

test_parallel_shell_func_timeout_fallback() {
	local fixture_file="$workdir/timeout-worker.sh"
	local input_file="$workdir/timeout-input.txt"
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

	if parallel_shell_func "$fixture_file" timeout_worker --lb <"$input_file"; then
		fail "Expected parallel_shell_func to surface timeout status 3"
	else
		status=$?
	fi

	[ "$status" -eq 3 ] || fail "Expected parallel_shell_func to return 3 after timeout, got $status"
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
test_run_owner_updates_halts_on_timeout

echo "Timeout propagation regression tests passed"