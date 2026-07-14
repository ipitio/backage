#!/bin/bash
# shellcheck disable=SC1091

set -euo pipefail

test_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
src_dir=$(cd "$test_dir/.." && pwd)
repo_dir=$(cd "$src_dir/.." && pwd)
tmp_dir=$(mktemp -d)
origin="$tmp_dir/origin.git"
seed="$tmp_dir/seed"
writer="$tmp_dir/writer"
signaler="$tmp_dir/signaler"

cleanup() {
	stop_workflow_handoff_monitor 2>/dev/null || true
	rm -rf "$tmp_dir"
}
trap cleanup EXIT

source "$src_dir/lib/handoff.sh"

stop_workflow="$repo_dir/.github/workflows/stop.yml"
grep -Fq "workflow_dispatch:" "$stop_workflow" || {
	echo "Stop workflow is not manually dispatchable" >&2
	exit 1
}
grep -Fq "bash src/lib/handoff.sh request" "$stop_workflow" || {
	echo "Stop workflow does not request a graceful handoff" >&2
	exit 1
}
if grep -Eq '^[[:space:]]+concurrency:' "$stop_workflow"; then
	echo "Stop workflow must not wait for publication concurrency" >&2
	exit 1
fi
if grep -Eq '^  update:' "$stop_workflow"; then
	echo "Stop workflow must not queue a replacement update" >&2
	exit 1
fi

git init --bare --initial-branch=master "$origin" >/dev/null
git init --initial-branch=master "$seed" >/dev/null
git -C "$seed" config user.name test
git -C "$seed" config user.email test@example.com
printf 'seed\n' >"$seed/README"
git -C "$seed" add README
git -C "$seed" commit -m seed >/dev/null
git -C "$seed" remote add origin "$origin"
git -C "$seed" push --quiet -u origin master
git clone --quiet "$origin" "$writer"
git clone --quiet "$origin" "$signaler"

export BKG_HANDOFF_CONTROL_REF=refs/heads/bkg-control
export BKG_HANDOFF_POLL_SECONDS=1
export GITHUB_RUN_ID=123

capture_workflow_handoff_baseline "$writer"
baseline=$BKG_HANDOFF_BASELINE_SHA
[ "$baseline" = "missing" ] || {
	echo "A missing control ref did not produce the expected baseline" >&2
	exit 1
}

BKG_ENV="$tmp_dir/env.env"
: >"$BKG_ENV"
set_BKG() {
	printf '%s=%s\n' "$1" "$2" >"$BKG_ENV"
}

assert_isolated_handoff_commit() {
	local repo=$1
	local commit=$2
	local expected_parent=${3:-}
	local empty_tree
	local message

	empty_tree=$(git -C "$repo" mktree </dev/null)
	[ "$(git -C "$repo" show -s --format=%T "$commit")" = "$empty_tree" ] || {
		echo "Handoff commit did not use the empty tree" >&2
		exit 1
	}
	[ "$(git -C "$repo" show -s --format=%P "$commit")" = "$expected_parent" ] || {
		echo "Handoff commit had unexpected ancestry" >&2
		exit 1
	}
	message=$(git -C "$repo" show -s --format=%B "$commit")
	[[ "$message" == *"$(workflow_handoff_format_marker)"* ]] || {
		echo "Handoff commit omitted the isolated-history marker" >&2
		exit 1
	}
}

signaler_head=$(git -C "$signaler" rev-parse HEAD)
signaler_user=$(git -C "$signaler" config user.name || :)
request_workflow_handoff "$signaler" >/dev/null
current=$(read_remote_handoff_sha "$signaler")
assert_isolated_handoff_commit "$signaler" "$current"
[ "$(git -C "$signaler" rev-parse HEAD)" = "$signaler_head" ] || {
	echo "Handoff request changed the caller's checkout" >&2
	exit 1
}
[ "$(git -C "$signaler" config user.name || :)" = "$signaler_user" ] || {
	echo "Handoff request changed the caller's Git identity" >&2
	exit 1
}
start_workflow_handoff_monitor "$writer"
for _ in 1 2 3 4 5; do
	grep -Fxq 'BKG_TIMEOUT=1' "$BKG_ENV" && break
	sleep 1
done
grep -Fxq 'BKG_TIMEOUT=1' "$BKG_ENV" || {
	echo "Control-ref creation did not request a graceful stop" >&2
	exit 1
}
stop_workflow_handoff_monitor

capture_workflow_handoff_baseline "$writer"
baseline=$BKG_HANDOFF_BASELINE_SHA
: >"$BKG_ENV"
request_workflow_handoff "$signaler" >/dev/null
current=$(read_remote_handoff_sha "$writer")
if [ -z "$current" ] || [ "$current" = "$baseline" ]; then
	echo "Second handoff request did not advance the control ref" >&2
	exit 1
fi
assert_isolated_handoff_commit "$signaler" "$current" "$baseline"
start_workflow_handoff_monitor "$writer"
for _ in 1 2 3 4 5; do
	grep -Fxq 'BKG_TIMEOUT=1' "$BKG_ENV" && break
	sleep 1
done
grep -Fxq 'BKG_TIMEOUT=1' "$BKG_ENV" || {
	echo "Handoff monitor did not request a graceful stop" >&2
	exit 1
}
stop_workflow_handoff_monitor

export BKG_HANDOFF_CONTROL_REF=refs/heads/legacy-control
git -C "$seed" push --quiet origin "HEAD:$BKG_HANDOFF_CONTROL_REF"
legacy=$(read_remote_handoff_sha "$signaler")
request_workflow_handoff "$signaler" >/dev/null
current=$(read_remote_handoff_sha "$signaler")
[ "$current" != "$legacy" ] || {
	echo "Legacy handoff ref was not migrated" >&2
	exit 1
}
assert_isolated_handoff_commit "$signaler" "$current"

export BKG_HANDOFF_CONTROL_REF=refs/heads/protected-legacy-control
git -C "$seed" push --quiet origin "HEAD:$BKG_HANDOFF_CONTROL_REF"
legacy=$(read_remote_handoff_sha "$signaler")
git -C "$origin" config receive.denyNonFastforwards true
request_workflow_handoff "$signaler" >/dev/null 2>"$tmp_dir/protected-migration.err"
current=$(read_remote_handoff_sha "$signaler")
[ "$(git -C "$signaler" show -s --format=%P "$current")" = "$legacy" ] || {
	echo "Protected legacy ref did not retain fast-forward ancestry" >&2
	exit 1
}
if workflow_handoff_tip_is_isolated "$signaler" "$current"; then
	echo "Protected legacy ref was incorrectly marked as isolated" >&2
	exit 1
fi
grep -Fq "could not be isolated" "$tmp_dir/protected-migration.err" || {
	echo "Protected legacy migration did not report its fallback" >&2
	exit 1
}
git -C "$origin" config receive.denyNonFastforwards false
export BKG_HANDOFF_CONTROL_REF=refs/heads/bkg-control

if BKG_HANDOFF_CONTROL_REF=refs/tags/not-allowed handoff_control_ref >/dev/null 2>&1; then
	echo "Handoff accepted a non-branch control ref" >&2
	exit 1
fi

scheduled_update_should_run current current 100 100 "" >/dev/null || {
	echo "Current scheduled update was rejected" >&2
	exit 1
}
scheduled_update_should_run current current 100 99 "" >/dev/null || {
	echo "A stale API response rejected the current scheduled update" >&2
	exit 1
}
if scheduled_update_should_run queued current 100 100 "" >/dev/null; then
	echo "Scheduled update ignored a newer Manual handoff" >&2
	exit 1
fi
if scheduled_update_should_run current current 100 101 "" >/dev/null; then
	echo "Superseded scheduled update was accepted" >&2
	exit 1
fi
if scheduled_update_should_run current current 100 100 200 >/dev/null; then
	echo "Scheduled update was accepted while a Manual run was waiting" >&2
	exit 1
fi

echo "Workflow handoff regression tests passed"
