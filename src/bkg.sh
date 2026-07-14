#!/bin/bash
# shellcheck disable=SC1091
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

	if declare -F stop_workflow_handoff_monitor >/dev/null; then
		stop_workflow_handoff_monitor
	fi
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
	local source_published_today=false
	local today_value

	today_value=$(date -u +%Y-%m-%d) || return 1
	if master_branch_has_commit_today "$today_value"; then
		source_published_today=true
	fi

	bkg_python run --source-published-today "$source_published_today" "$@"
}
