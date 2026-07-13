#!/bin/bash
# shellcheck disable=SC1091

set -euo pipefail

test_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
src_dir=$(cd "$test_dir/.." && pwd)
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

request_workflow_handoff "$signaler" >/dev/null
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

if BKG_HANDOFF_CONTROL_REF=refs/tags/not-allowed handoff_control_ref >/dev/null 2>&1; then
	echo "Handoff accepted a non-branch control ref" >&2
	exit 1
fi

echo "Workflow handoff regression tests passed"
