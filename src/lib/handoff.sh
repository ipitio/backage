#!/bin/bash
# Workflow handoff signaling and active-run monitoring.

handoff_control_ref() {
	local control_ref=${BKG_HANDOFF_CONTROL_REF:-}

	[[ "$control_ref" == refs/heads/* ]] || {
		echo "BKG_HANDOFF_CONTROL_REF must name a branch under refs/heads" >&2
		return 1
	}
	printf '%s\n' "$control_ref"
}

read_remote_handoff_sha() {
	local repo=${1:-.}
	local control_ref
	local output
	local timeout_seconds=${BKG_HANDOFF_GIT_TIMEOUT_SECONDS:-20}

	control_ref=$(handoff_control_ref) || return 1
	[[ "$timeout_seconds" =~ ^[1-9][0-9]*$ ]] || timeout_seconds=20
	output=$(timeout "$timeout_seconds" git -C "$repo" ls-remote --refs origin "$control_ref" 2>/dev/null) || return 1
	awk 'NR == 1 { print $1 }' <<<"$output"
}

request_workflow_handoff() {
	local repo=${1:-.}
	local control_ref
	local remote_sha
	local base
	local attempt

	control_ref=$(handoff_control_ref) || return 1
	git -C "$repo" config user.name "${GITHUB_ACTOR:-github-actions[bot]}"
	git -C "$repo" config user.email "${GITHUB_ACTOR:-41898282+github-actions[bot]}@users.noreply.github.com"

	for attempt in 1 2 3; do
		remote_sha=$(read_remote_handoff_sha "$repo") || {
			echo "Failed to read workflow handoff ref" >&2
			return 1
		}
		base=$(git -C "$repo" rev-parse HEAD) || return 1
		if [ -n "$remote_sha" ]; then
			git -C "$repo" fetch --quiet --no-tags --depth=1 origin "$control_ref" || continue
			base=$(git -C "$repo" rev-parse FETCH_HEAD) || continue
		fi

		git -C "$repo" switch --quiet --detach "$base" || return 1
		git -C "$repo" commit --allow-empty \
			-m "Request workflow handoff (${GITHUB_RUN_ID:-manual})" >/dev/null || return 1
		if git -C "$repo" push --quiet origin "HEAD:$control_ref"; then
			echo "Requested graceful handoff from the active update"
			return 0
		fi
		echo "Workflow handoff ref changed concurrently; retrying ($attempt/3)" >&2
	done

	echo "Failed to request workflow handoff after 3 attempts" >&2
	return 1
}

capture_workflow_handoff_baseline() {
	local repo=${1:-.}
	local baseline

	[ -n "${BKG_HANDOFF_CONTROL_REF:-}" ] || return 0
	baseline=$(read_remote_handoff_sha "$repo") || {
		echo "Failed to capture workflow handoff baseline; handoff disabled for this run" >&2
		unset BKG_HANDOFF_BASELINE_SHA
		return 0
	}
	BKG_HANDOFF_BASELINE_SHA=${baseline:-missing}
	export BKG_HANDOFF_BASELINE_SHA
}

monitor_workflow_handoff() {
	local repo=${1:-.}
	local baseline=${BKG_HANDOFF_BASELINE_SHA:-}
	local current
	local poll_seconds=${BKG_HANDOFF_POLL_SECONDS:-60}
	local reported_failure=false

	[ -n "$baseline" ] || return 0
	[[ "$poll_seconds" =~ ^[1-9][0-9]*$ ]] || poll_seconds=60

	while true; do
		if current=$(read_remote_handoff_sha "$repo"); then
			current=${current:-missing}
			reported_failure=false
			if [ "$current" != "$baseline" ]; then
				echo "Workflow handoff requested; stopping gracefully before the next publication"
				set_BKG BKG_TIMEOUT "1"
				return 0
			fi
		elif ! $reported_failure; then
			echo "Failed to check workflow handoff ref; the active update will continue" >&2
			reported_failure=true
		fi
		sleep "$poll_seconds"
	done
}

start_workflow_handoff_monitor() {
	local repo=${1:-.}

	[ -n "${BKG_HANDOFF_BASELINE_SHA:-}" ] || return 0
	[ -z "${BKG_HANDOFF_MONITOR_PID:-}" ] || return 0
	monitor_workflow_handoff "$repo" &
	BKG_HANDOFF_MONITOR_PID=$!
}

stop_workflow_handoff_monitor() {
	local pid=${BKG_HANDOFF_MONITOR_PID:-}

	[ -n "$pid" ] || return 0
	if kill -0 "$pid" 2>/dev/null; then
		if declare -F terminate_process_tree >/dev/null; then
			terminate_process_tree "$pid"
		else
			kill "$pid" 2>/dev/null || true
		fi
	fi
	wait "$pid" 2>/dev/null || true
	unset BKG_HANDOFF_MONITOR_PID
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
	set -euo pipefail
	command=${1:-}
	shift || true
	case "$command" in
	request)
		request_workflow_handoff "${1:-.}"
		;;
	*)
		echo "Usage: $0 request [REPOSITORY]" >&2
		exit 2
		;;
	esac
fi
