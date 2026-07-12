#!/bin/bash
# shellcheck disable=SC1091,SC2015,SC2034
#
# Modes:
# 0 - Update all public pkgs (default)
# 1 - Update own public pkgs
# 2 - Just clean the dir
# 3 - Update all public and own private pkgs
# 4 - Update own public and private pkgs
# 5 - Update own private pkgs
#
# Duration:
#  < 0 - Unlimited
# >= 0 - Run for this many seconds
#
# Usage: source bkg.sh && main [-d <duration>] [-m <mode>]

source lib/owner.sh

run_owner_updates() {
	local owners_queue
	local batch_first_started
	local batch_marker
	owners_queue=$(get_BKG_set BKG_OWNERS_QUEUE)
	[ -n "$owners_queue" ] || return 0
	batch_first_started=$(current_batch_first_started)
	[ -n "$batch_first_started" ] || batch_first_started="0000-00-00"
	batch_marker=$(get_BKG BKG_BATCH_MARKER)
	[ -n "$batch_marker" ] || return 1
	bkg_python orchestration update-owners \
		"$batch_first_started" "$batch_marker" "${fast_out:-false}"
}

handle_owner_update_status() {
	local phase_status=${1:-0}
	local decision
	local action
	local decided_status
	local message

	decision=$(bkg_python orchestration owner-phase-decision "$phase_status" "$return_code") || return $?
	IFS=$'\t' read -r action decided_status message <<<"$decision"
	[[ "$decided_status" =~ ^[0-9]+$ ]] || {
		echo "Invalid owner phase decision from Python: $decision" >&2
		return 1
	}
	case "$action" in
	publish)
		return_code=$decided_status
		[ -z "$message" ] || echo "$message"
		return 0
		;;
	abort)
		[ -z "$message" ] || echo "$message" >&2
		return "$decided_status"
		;;
	*)
		echo "Invalid owner phase decision from Python: $decision" >&2
		return 1
		;;
	esac
}

run_owner_page_discovery() {
	local page=1
	local max_pages=${BKG_OWNER_DISCOVERY_MAX_PAGES:-1}
	local status=0

	while ((page <= max_pages)); do
		page_owner "$page"
		status=$?

		if ((status == 0)); then
			((page++))
			continue
		fi

		if ((status == 2)); then
			return 0
		fi

		return "$status"
	done

	return 0
}

startup_phase_started_at() {
	date -u +%s
}

log_startup_phase() {
	local phase=$1
	local started_at=${2:-0}
	local elapsed=0

	((started_at > 0)) || return 0
	elapsed=$(( $(date -u +%s) - started_at ))
	echo "Startup phase '$phase' completed in ${elapsed}s"
}

log_prequeue_elapsed_once() {
	[ "${BKG_QUEUE_START_LOGGED:-0}" = "1" ] && return 0
	BKG_QUEUE_START_LOGGED=1
	log_startup_phase "pre-queue-work" "${BKG_STARTUP_STARTED_AT:-0}"
}

prepare_package_plan() {
	local since=$1
	local directory=${2:-.}
	local reset=${3:-false}
	local summary
	local total
	local completed
	local pending

	summary=$(bkg_python orchestration prepare-package-plan "$since" "$directory" "$reset") || return $?
	IFS=$'\t' read -r total completed pending <<<"$summary"
	[[ "$total" =~ ^[0-9]+$ && "$completed" =~ ^[0-9]+$ && "$pending" =~ ^[0-9]+$ ]] || {
		echo "Invalid package plan summary from Python: $summary" >&2
		return 1
	}
	printf '%s\t%s\t%s\n' "$total" "$completed" "$pending"
}

prepare_run() {
	local today_value=$1
	local started_at=$2
	local directory=${3:-.}
	local summary
	local batch_started
	local total
	local completed
	local pending
	local database_size
	local opted_out_count
	local fast_out_value

	summary=$(bkg_python orchestration prepare-run "$today_value" "$started_at" "$directory") || return $?
	IFS=$'\t' read -r batch_started total completed pending database_size opted_out_count fast_out_value <<<"$summary"
	[[ "$total" =~ ^[0-9]+$ && "$completed" =~ ^[0-9]+$ && "$pending" =~ ^[0-9]+$ ]] || {
		echo "Invalid startup package summary from Python: $summary" >&2
		return 1
	}
	[[ "$database_size" =~ ^[0-9]+$ && "$opted_out_count" =~ ^[0-9]+$ ]] || {
		echo "Invalid startup storage summary from Python: $summary" >&2
		return 1
	}
	[ -n "$batch_started" ] || {
		echo "Python startup omitted the batch start date" >&2
		return 1
	}
	[ "$fast_out_value" = "true" ] || [ "$fast_out_value" = "false" ] || {
		echo "Invalid startup opt-out transition from Python: $summary" >&2
		return 1
	}
	printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
		"$batch_started" "$total" "$completed" "$pending" \
		"$database_size" "$opted_out_count" "$fast_out_value"
}

sync_batch_progress() {
	local today_value=$1
	local total=$2
	local completed=$3
	local transition

	transition=$(bkg_python orchestration complete-batch-if-exhausted "$today_value" "$total" "$completed") || return $?
	IFS=$'\t' read -r BKG_BATCH_RESET BKG_BATCH_FIRST_STARTED <<<"$transition"
	if [ "$BKG_BATCH_RESET" != "true" ] && [ "$BKG_BATCH_RESET" != "false" ]; then
		echo "Invalid batch transition from Python: $transition" >&2
		return 1
	fi
	[ -n "$BKG_BATCH_FIRST_STARTED" ] || {
		echo "Python batch transition omitted the batch start date" >&2
		return 1
	}
}

db_restore_signature_file() {
	printf '%s\n' "${BKG_INDEX_DB}.snapshot.sha256"
}

current_index_snapshot_archive_file() {
	bkg_python snapshot current-archive
}

post_stop_bkg_python() {
	local previous_timeout
	local previous_max_len=$BKG_MAX_LEN
	local status=0

	previous_timeout=$(get_BKG BKG_TIMEOUT)
	set_BKG BKG_TIMEOUT "0"
	BKG_MAX_LEN=0 bkg_python "$@" || status=$?
	BKG_MAX_LEN=$previous_max_len

	if ((status == 3)); then
		set_BKG BKG_TIMEOUT "1"
	elif [ -n "$previous_timeout" ]; then
		set_BKG BKG_TIMEOUT "$previous_timeout"
	else
		del_BKG BKG_TIMEOUT
	fi
	return "$status"
}

post_stop_current_index_snapshot_archive_file() {
	post_stop_bkg_python snapshot current-archive
}

startup_index_snapshot_archive_file() {
	local snapshot_file

	snapshot_file=$(current_index_snapshot_archive_file 2>/dev/null || :)
	if [ -n "$snapshot_file" ]; then
		printf '%s\n' "$snapshot_file"
		return 0
	fi

	[ -n "${BKG_INDEX_DB:-}" ] || return 1
	snapshot_file="$(dirname "$BKG_INDEX_DB")/.snapshot/$(basename "$BKG_INDEX_DB")"
	[ -f "$snapshot_file" ] || return 1
	printf '%s\n' "$snapshot_file"
}

current_index_snapshot_signature() {
	bkg_python snapshot current-signature
}

restore_db_from_index_snapshot_if_needed() {
	bkg_python snapshot restore-if-needed
}

restore_startup_database_snapshot_if_needed() {
	local snapshot_file=${1:-}
	local output
	local status=0

	[ -n "$snapshot_file" ] || return 0
	set_BKG BKG_SCRIPT_START "$(date -u +%s)"
	set_BKG BKG_TIMEOUT "0"
	output=$(bkg_python snapshot restore-archive-if-needed "$snapshot_file" 2>&1) || status=$?
	[ -z "$output" ] || printf '%s\n' "$output"
	if ((status != 0)) && [ -z "$output" ]; then
		echo "Snapshot restore command failed with status $status for $snapshot_file" >&2
	fi
	return "$status"
}

index_database_owner_count() {
	sqlite3 "$BKG_INDEX_DB" "SELECT COUNT(DISTINCT owner) FROM $BKG_INDEX_TBL_PKG" 2>/dev/null || echo 0
}

write_db_restore_signature() {
	bkg_python snapshot write-restore-signature >/dev/null || :
}

checkpoint_database_for_archive() {
	post_stop_bkg_python snapshot checkpoint >/dev/null || :
}

prepare_database_snapshot_for_archive() {
	post_stop_bkg_python snapshot prepare >/dev/null
}

rotate_database_snapshot_if_needed() {
	local threshold_bytes=${1:-2000000000}
	local batch_first_started=${2:-}
	local date_stamp=${3:-}

	[ -n "$batch_first_started" ] || batch_first_started=$(current_batch_first_started)
	[ -n "$batch_first_started" ] || batch_first_started="0000-00-00"
	[ -n "$date_stamp" ] || date_stamp=$(date -u +%Y.%m.%d)
	post_stop_bkg_python snapshot rotate-if-needed "$threshold_bytes" "$batch_first_started" "$date_stamp" >/dev/null
}

drop_replaced_legacy_version_tables() {
	local batch_first_started=${1:-}

	[ -n "${BKG_INDEX_DB:-}" ] || return 0
	[ -n "$batch_first_started" ] || batch_first_started=$(current_batch_first_started)
	[ -n "$batch_first_started" ] || batch_first_started="0000-00-00"
	bkg_python database cleanup-legacy-all "$batch_first_started" >/dev/null
}

main() {
	local pkg_all
	local pkg_done
	local pkg_left
	local db_size_curr
	local opted_out
	local connections
	local return_code=0
	local phase_status=0
	local include_manual=true
	local rest_first
	local skip_explore=false
	local request_limit=100
	local phase_started_at=0
	local startup_summary
	connections=$(mktemp) || exit 1

	while getopts "d:m:" flag; do
		case ${flag} in
		d)
			BKG_MAX_LEN=$((OPTARG))
			;;
		m)
			BKG_MODE=$((OPTARG))
			;;
		?)
			echo "Invalid option found: -${OPTARG}."
			exit 1
			;;
		esac
	done

	today=$(date -u +%Y-%m-%d)
	BKG_SCRIPT_START=$(date -u +%s)
	BKG_STARTUP_STARTED_AT=$BKG_SCRIPT_START
	BKG_QUEUE_START_LOGGED=0
	startup_summary=$(prepare_run "$today" "$BKG_SCRIPT_START" ".") || return $?
	IFS=$'\t' read -r BKG_BATCH_FIRST_STARTED pkg_all pkg_done pkg_left db_size_curr opted_out fast_out <<<"$startup_summary"
	echo "all: $pkg_all"
	echo "done: $pkg_done"
	echo "left: $pkg_left"

	if [ "$BKG_MODE" -ne 2 ]; then
		if [ "$BKG_MODE" -eq 0 ] || [ "$BKG_MODE" -eq 3 ]; then
			if $fast_out; then
				log_prequeue_elapsed_once
				bkg_python orchestration prepare-optout-owner-queue
				phase_status=$?
				if ((phase_status == 3)); then
					return_code=3
				elif ((phase_status != 0)); then
					return "$phase_status"
				else
					return_code=1
				fi
			else
				skip_explore=false
				if [ "$GITHUB_OWNER" = "ipitio" ] && daily_gate_should_skip_today BKG_LAST_EXPLORE_DATE "$today"; then
					skip_explore=true
				fi
				bkg_python orchestration discover-owners \
					"$today" "$skip_explore" "$connections" packages_all
				phase_status=$?
				if ((phase_status == 3)); then
					return_code=3
				elif ((phase_status != 0)); then
					return "$phase_status"
				fi

				if ((return_code == 3)); then
					echo "Reached BKG_MAX_LEN, stopping after persisting state..."
				else
					sync_batch_progress "$today" "$pkg_all" "$pkg_done" || return $?
					if $BKG_BATCH_RESET; then
						prepare_package_plan "$BKG_BATCH_FIRST_STARTED" "." >/dev/null || return $?
					fi

					rest_first=$(get_BKG BKG_REST_TO_TOP)
					log_prequeue_elapsed_once
					phase_started_at=$(startup_phase_started_at)
					include_manual=true
					if daily_gate_should_skip_today BKG_LAST_OWNERS_QUEUE_DATE "$today"; then
						include_manual=false
						echo "Skipping owners.txt queue; already ran today"
					fi
					bkg_python orchestration prepare-owner-queue \
						"$rest_first" "$connections" "$request_limit" \
						"$include_manual" "." "$(date -u +%s)"
					phase_status=$?
					if ((phase_status == 3)); then
						return_code=3
					elif ((phase_status != 0)); then
						return "$phase_status"
					fi
					if $include_manual && ((return_code != 3)); then
						mark_daily_gate_completed BKG_LAST_OWNERS_QUEUE_DATE "$today"
					fi
					log_startup_phase "queue-discovered-owners" "$phase_started_at"
					rm -f all_owners_in_db all_owners_tu owners_updated owners_partially_updated owners_stale owners_scanned_without_packages
					set_BKG BKG_DIFF "$db_size_curr"
					set_BKG BKG_REST_TO_TOP "$((1 - rest_first))"
				fi
			fi
		else
			log_prequeue_elapsed_once
			phase_started_at=$(startup_phase_started_at)
			bkg_python orchestration discover-owners \
				"$today" false "$connections" packages_all
			phase_status=$?
			if ((phase_status == 3)); then
				return_code=3
			elif ((phase_status != 0)); then
				return "$phase_status"
			fi
			if ((return_code != 3)); then
				bkg_python orchestration prepare-targeted-owner-queue "$connections"
				phase_status=$?
				if ((phase_status == 3)); then
					return_code=3
				elif ((phase_status != 0)); then
					return "$phase_status"
				fi
			fi
			log_startup_phase "queue-membership-owners" "$phase_started_at"
		fi

		rm -f "$connections"
		BKG_BATCH_FIRST_STARTED=$(get_BKG BKG_BATCH_FIRST_STARTED)
		# BKG_INDEX_DIR is initialized by the update.sh entrypoint.
		# shellcheck disable=SC2153
		[ -d "$BKG_INDEX_DIR" ] || mkdir "$BKG_INDEX_DIR"

		if ((return_code != 3)); then
			local queued_owner_file
			local queued_owner_count
			local materialize_started_at
			queued_owner_file=$(mktemp) || return 1
			materialize_started_at=$(startup_phase_started_at)
			index_queue_owner_names >"$queued_owner_file"
			queued_owner_count=$(awk 'NF' "$queued_owner_file" | wc -l)
			echo "Materializing $queued_owner_count queued owner tree(s)..."

			if [ -s "$queued_owner_file" ]; then
				index_sparse_add_paths <"$queued_owner_file" || {
					rm -f "$queued_owner_file"
					return $?
				}
			fi
			rm -f "$queued_owner_file"
			log_startup_phase "materialize-queued-owner-trees" "$materialize_started_at"

			run_owner_updates
			phase_status=$?
			handle_owner_update_status "$phase_status" || return $?
		fi

	fi

	if [ "$BKG_MODE" -ne 2 ]; then
		post_stop_bkg_python orchestration finalize-run "$today" true "." || return $?
	else
		post_stop_bkg_python orchestration finalize-run "$today" false "." || return $?
	fi
	del_BKG BKG_TIMEOUT
	return $return_code
}
