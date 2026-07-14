#!/bin/bash
# Emit deterministic legacy-main phase and state results for parity tests.
# shellcheck disable=SC1091

set -euo pipefail

repo=$1
workdir=$2
mode=$3
fast_out_case=$4
stop_at=$5
owner_status=$6
batch_reset=$7
daily_skip=$8
events_file=$workdir/events

export BKG_SKIP_DEP_VERIFY=1
export BKG_UTIL_BOOTSTRAPPED=1
cd "$repo/src"
source bkg.sh
cd "$workdir"

declare -A STATE=()
STATE[BKG_BATCH_FIRST_STARTED]=2026-07-12
STATE[BKG_BATCH_MARKER]=batch-1
STATE[BKG_REST_TO_TOP]=0
STATE[BKG_TIMEOUT]=0
: >"$events_file"

trace() {
	printf '%s\n' "$1" >>"$events_file"
}

get_BKG() {
	printf '%s\n' "${STATE[$1]-}"
}

get_BKG_set() {
	printf '%s\n' "${STATE[$1]-}"
}

set_BKG() {
	STATE[$1]=$2
}

del_BKG() {
	unset "STATE[$1]"
}

prepare_run() {
	trace prepare
	STATE[BKG_BATCH_FIRST_STARTED]=2026-07-12
	STATE[BKG_BATCH_MARKER]=batch-1
	STATE[BKG_TIMEOUT]=0
	if $batch_reset; then
		printf '2026-07-12\t2\t2\t0\t1234\t0\t%s\n' "$fast_out_case"
	else
		printf '2026-07-12\t10\t2\t8\t1234\t0\t%s\n' "$fast_out_case"
	fi
}

prepare_package_plan() {
	trace package-plan
	printf '10\t0\t10\n'
}

sync_batch_progress() {
	if $batch_reset; then
		BKG_BATCH_RESET=true
		BKG_BATCH_FIRST_STARTED=$1
		STATE[BKG_BATCH_FIRST_STARTED]=$1
		STATE[BKG_BATCH_MARKER]=batch-2
	else
		# Read by the sourced main function after this override returns.
		# shellcheck disable=SC2034
		BKG_BATCH_RESET=false
		BKG_BATCH_FIRST_STARTED=${STATE[BKG_BATCH_FIRST_STARTED]}
	fi
}

daily_gate_should_skip_today() {
	$daily_skip
}

mark_daily_gate_completed() {
	:
}

bkg_python() {
	local operation="${1:-} ${2:-}"
	case "$operation" in
	"orchestration discover-owners")
		trace "discover:${4:-false}"
		[ "$stop_at" != discover ] || return 3
		;;
	"orchestration prepare-optout-owner-queue")
		trace optout-queue
		[ "$stop_at" != optout-queue ] || return 3
		STATE[BKG_OWNERS_QUEUE]=$'1/one\n2/two\n3/one'
		;;
	"orchestration prepare-owner-queue")
		trace "owner-queue:${6:-true}"
		[ "$stop_at" != owner-queue ] || return 3
		STATE[BKG_OWNERS_QUEUE]=$'1/one\n2/two\n3/one'
		;;
	"orchestration prepare-targeted-owner-queue")
		trace targeted-queue
		[ "$stop_at" != targeted-queue ] || return 3
		STATE[BKG_OWNERS_QUEUE]=$'1/one\n2/two\n3/one'
		;;
	*)
		printf 'Unexpected Python operation: %s\n' "$*" >&2
		return 1
		;;
	esac
}

index_queue_owner_names() {
	printf 'one\ntwo\n'
}

index_sparse_add_paths() {
	cat >/dev/null
	trace materialize
}

run_owner_updates() {
	trace update
	[ "$stop_at" != update ] || return 3
	return "$owner_status"
}

handle_owner_update_status() {
	local status=$1
	case "$status" in
	0)
		return 0
		;;
	3)
		# Dynamically scoped local from the sourced main function.
		# shellcheck disable=SC2034
		return_code=3
		return 0
		;;
	*)
		return "$status"
		;;
	esac
}

post_stop_bkg_python() {
	[ "${1:-} ${2:-}" = "orchestration finalize-run" ] || return 1
	trace "finalize:${4:-false}"
}

startup_phase_started_at() {
	printf '1000\n'
}

log_startup_phase() {
	:
}

log_prequeue_elapsed_once() {
	:
}

export GITHUB_OWNER=example
$daily_skip && export GITHUB_OWNER=ipitio
export BKG_MODE=$mode
export BKG_MAX_LEN=14400
BKG_INDEX_DIR=$workdir/index
mkdir -p "$BKG_INDEX_DIR"
OPTIND=1

set +e
main
status=$?
set -e

printf 'status\t%s\n' "$status"
while IFS= read -r event; do
	printf 'event\t%s\n' "$event"
done <"$events_file"
printf 'state\tBKG_DIFF\t%s\n' "${STATE[BKG_DIFF]-<missing>}"
printf 'state\tBKG_REST_TO_TOP\t%s\n' "${STATE[BKG_REST_TO_TOP]-<missing>}"
printf 'state\tBKG_TIMEOUT\t%s\n' "${STATE[BKG_TIMEOUT]-<missing>}"
