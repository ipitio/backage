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

owner_update_wait_notice() {
	local started_at=${1:-0}
	local last_notice_at=${2:-0}
	local now
	local elapsed
	local notice_interval=300

	OWNER_UPDATE_WAIT_STARTED=$started_at
	OWNER_UPDATE_WAIT_LAST_NOTICE=$last_notice_at
	OWNER_UPDATE_WAIT_MESSAGE=""
	now=$(date -u +%s)

	if ((OWNER_UPDATE_WAIT_STARTED == 0)); then
		OWNER_UPDATE_WAIT_STARTED=$now
		OWNER_UPDATE_WAIT_LAST_NOTICE=$now
		OWNER_UPDATE_WAIT_MESSAGE="Waiting for active owner updates to stop..."
		return
	fi

	if ((now - OWNER_UPDATE_WAIT_LAST_NOTICE < notice_interval)); then
		return
	fi

	OWNER_UPDATE_WAIT_LAST_NOTICE=$now
	elapsed=$((now - OWNER_UPDATE_WAIT_STARTED))
	OWNER_UPDATE_WAIT_MESSAGE="Still waiting for active owner updates to stop after ${elapsed}s..."
}

owner_update_force_stop_due() {
	local started_at=${1:-0}
	local grace_period=${2:-180}
	local now

	OWNER_UPDATE_FORCE_STOP_DUE=false
	((started_at > 0)) || return
	now=$(date -u +%s)
	if ((now - started_at >= grace_period)); then
		OWNER_UPDATE_FORCE_STOP_DUE=true
	fi
}

owner_update_collect_child_pids() {
	local root_pid=$1
	local child_pid

	[ -n "$root_pid" ] || return

	while IFS= read -r child_pid; do
		child_pid=$(awk '{print $1}' <<<"$child_pid")
		[ -n "$child_pid" ] || continue
		printf '%s\n' "$child_pid"
		owner_update_collect_child_pids "$child_pid"
	done < <(ps -o pid= --ppid "$root_pid" 2>/dev/null)
}

owner_update_force_stop() {
	local root_pid=$1
	local pid
	local -a pids=()

	[ -n "$root_pid" ] || return

	while IFS= read -r pid; do
		[ -n "$pid" ] || continue
		pids+=("$pid")
	done < <(owner_update_collect_child_pids "$root_pid")

	pids+=("$root_pid")
	terminate_pids_with_grace "${pids[@]}"
}

run_owner_updates() {
	local owners_queue
	local status=0
	local updates_pid=""
	local stop_wait_started=0
	local last_wait_notice=0
	local graceful_stop_window=${BKG_OWNER_UPDATE_STOP_GRACE:-180}
	local forced_stop=false
	local elapsed=0
	owners_queue=$(get_BKG_set BKG_OWNERS_QUEUE)
	[ -n "$owners_queue" ] || return 0

	if [[ "$GITHUB_OWNER" = "ipitio" && "$(git branch --show-current)" = "master" ]]; then
		(
			printf '%s\n' "$owners_queue" | parallel_shell_func "$BKG_ROOT/src/lib/owner.sh" update_owner --lb --halt soon,fail=1
		) &
		updates_pid=$!

		while background_job_running "$updates_pid"; do
			sleep 1
			background_job_running "$updates_pid" || break
			script_stop_requested || continue
			owner_update_wait_notice "$stop_wait_started" "$last_wait_notice"
			stop_wait_started=$OWNER_UPDATE_WAIT_STARTED
			last_wait_notice=$OWNER_UPDATE_WAIT_LAST_NOTICE
			[ -z "$OWNER_UPDATE_WAIT_MESSAGE" ] || echo "$OWNER_UPDATE_WAIT_MESSAGE"

			owner_update_force_stop_due "$stop_wait_started" "$graceful_stop_window"
			if ! $forced_stop && $OWNER_UPDATE_FORCE_STOP_DUE; then
				elapsed=$(( $(date -u +%s) - stop_wait_started ))
				echo "Graceful stop window exceeded after ${elapsed}s; force-stopping active owner updates..."
				owner_update_force_stop "$updates_pid"
				forced_stop=true
			fi
		done

		wait "$updates_pid"
		status=$?
		if $forced_stop && [ "$(get_BKG BKG_TIMEOUT)" = "1" ]; then
			status=3
		fi
	else # typically fewer owners
		run_parallel update_owner "$owners_queue"
		status=$?
	fi

	return "$status"
}

handle_owner_update_status() {
	local phase_status=${1:-0}

	if ((phase_status == 3)); then
		return_code=3
		echo "Reached BKG_MAX_LEN, stopping after persisting state..."
		return 0
	fi

	if ((phase_status != 0)); then
		echo "Owner updates failed with status $phase_status; stopping before snapshot publication." >&2
		return "$phase_status"
	fi

	return 0
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

batch_should_reset() {
	local remaining=${1:-0}

	((remaining == 0))
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

post_stop_ytox() {
	[ -n "$1" ] || return 1
	post_stop_bkg_python json-to-xml "$1" >/dev/null
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
	local rotated=false
	local owners
	local repos
	local packages
	local pkg_done
	local pkg_left
	local db_size_curr
	local db_size_prev
	local connections
	local return_code=0
	local phase_status=0
	local opted_out
	local opted_out_before
	local owners_queue_source
	local rest_first
	local request_limit=100
	local phase_started_at=0
	local owners_table_sql
	local packages_table_sql
	local batch_first_started_sql
	connections=$(mktemp) || exit 1
	temp_connections=$(mktemp) || exit 1

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
	[ -n "$(get_BKG BKG_BATCH_FIRST_STARTED)" ] || set_BKG BKG_BATCH_FIRST_STARTED "$today"
	[ -n "$(get_BKG BKG_RATE_LIMIT_START)" ] || set_BKG BKG_RATE_LIMIT_START "$(date -u +%s)"
	[ -n "$(get_BKG BKG_MIN_RATE_LIMIT_START)" ] || set_BKG BKG_MIN_RATE_LIMIT_START "$(date -u +%s)"
	[ -n "$(get_BKG BKG_CALLS_TO_API)" ] || set_BKG BKG_CALLS_TO_API "0"
	[ -n "$(get_BKG BKG_MIN_CALLS_TO_API)" ] || set_BKG BKG_MIN_CALLS_TO_API "0"
	[ -n "$(get_BKG BKG_LAST_SCANNED_ID)" ] || set_BKG BKG_LAST_SCANNED_ID "0"
	[ -n "$(get_BKG BKG_DIFF)" ] || set_BKG BKG_DIFF "0"
	[ -n "$(get_BKG BKG_REST_TO_TOP)" ] || set_BKG BKG_REST_TO_TOP "0"
	[ -n "$(get_BKG BKG_BATCH_MARKER)" ] || set_BKG BKG_BATCH_MARKER "$(generate_batch_marker)"
	BKG_BATCH_FIRST_STARTED=$(get_BKG BKG_BATCH_FIRST_STARTED)
	reset_owner_id_cache || return 1
	set_BKG BKG_DISCOVERED_CONNECTION_OWNERS ""
	set_BKG BKG_OWNERS_QUEUE ""
	set_BKG BKG_TIMEOUT "0"
	set_BKG BKG_SCRIPT_START "$BKG_SCRIPT_START"

	# reset the rate limit if an hour has passed since the last run started
	if (($(get_BKG BKG_RATE_LIMIT_START) + 3600 <= $(date -u +%s))); then
		set_BKG BKG_RATE_LIMIT_START "$(date -u +%s)"
		set_BKG BKG_CALLS_TO_API "0"
	fi

	# reset the secondary rate limit if a minute has passed since the last run started
	if (($(get_BKG BKG_MIN_RATE_LIMIT_START) + 60 <= $(date -u +%s))); then
		set_BKG BKG_MIN_RATE_LIMIT_START "$(date -u +%s)"
		set_BKG BKG_MIN_CALLS_TO_API "0"
	fi

	if current_index_snapshot_archive_file >/dev/null 2>&1; then
		phase_started_at=$(startup_phase_started_at)
		restore_db_from_index_snapshot_if_needed || :
		log_startup_phase "restore-db-from-snapshot" "$phase_started_at"
	fi

	[ -f "$BKG_INDEX_DB" ] || {
		[ -f "$BKG_INDEX_DB".bak ] && mv "$BKG_INDEX_DB".bak "$BKG_INDEX_DB" || sqlite3 "$BKG_INDEX_DB" ""
	}
	phase_started_at=$(startup_phase_started_at)
	sqlite_ensure_index_schema || return $?
	owners_table_sql=$(sqlite_quote_identifier "$BKG_INDEX_TBL_OWN")
	packages_table_sql=$(sqlite_quote_identifier "$BKG_INDEX_TBL_PKG")
	batch_first_started_sql=$(sqlite_quote_literal "$BKG_BATCH_FIRST_STARTED")
	sqlite3 "$BKG_INDEX_DB" "
		select current.owner_id, current.owner, current.repo, current.package,
		       max(current.date) as max_date
		from $packages_table_sql current
		where not exists (
			select 1
			from bkg_package_publications pending
			where pending.owner_id = current.owner_id
			  and pending.owner_type = current.owner_type
			  and pending.package_type = current.package_type
			  and pending.owner = current.owner
			  and pending.repo = current.repo
			  and pending.package = current.package
		)
		group by current.owner_id, current.owner, current.repo, current.package
		having max(current.date) >= $batch_first_started_sql
		order by max_date asc;
	" >packages_already_updated
	sqlite3 "$BKG_INDEX_DB" "select owner_id, owner, repo, package, max(date) as max_date from $packages_table_sql group by owner_id, owner, repo, package order by date asc;" >packages_all
	sqlite3 "$BKG_INDEX_DB" "
		select owner
		from (
			select owner, min(date) as first_date
			from $packages_table_sql
			group by owner
			union all
			select owner, min(date) as first_date
			from $owners_table_sql
			where date >= $batch_first_started_sql
			group by owner
		)
		group by owner
		order by min(first_date), owner;
	" >all_owners_in_db
	grep -vFxf packages_already_updated packages_all >packages_to_update
	pkg_done=$(wc -l <packages_already_updated)
	pkg_left=$(wc -l <packages_to_update)
	echo "all: $(wc -l <packages_all)"
	echo "done: $pkg_done"
	echo "left: $pkg_left"
	db_size_curr=$(stat -c %s "$BKG_INDEX_DB")
	db_size_prev=$(get_BKG BKG_DIFF)
	[ -n "$db_size_curr" ] || db_size_curr=0
	[ -n "$db_size_prev" ] || db_size_prev=0
	clean_owners "$BKG_OPTOUT"
	opted_out=$(wc -l <"$BKG_OPTOUT")
	opted_out_before=$(get_BKG BKG_OUT)
	fast_out=$([ "$GITHUB_OWNER" = "ipitio" ] && [ -n "$opted_out_before" ] && ((opted_out_before < opted_out)) && echo "true" || echo "false")
	log_startup_phase "prepare-package-state" "$phase_started_at"

	if [ "$BKG_MODE" -ne 2 ]; then
		if [ "$BKG_MODE" -eq 0 ] || [ "$BKG_MODE" -eq 3 ]; then
			if $fast_out; then
				log_prequeue_elapsed_once
				grep -oP '^[^\/]+' "$BKG_OPTOUT" | parallel_shell_func "$BKG_ROOT/src/lib/owner.sh" save_owner --lb
				return_code=1
			else
				if [ "$GITHUB_OWNER" = "ipitio" ]; then
					if daily_gate_should_skip_today BKG_LAST_EXPLORE_DATE "$today"; then
						: >"$connections"
						echo "Skipping explore; already ran today"
					else
						phase_started_at=$(startup_phase_started_at)
						explore "$GITHUB_OWNER" >"$connections"
						phase_status=$?
						((phase_status != 3)) || return_code=3
						explore "$GITHUB_OWNER/$GITHUB_REPO" >>"$connections"
						phase_status=$?
						((phase_status != 3)) || return_code=3
						log_startup_phase "discover-connections" "$phase_started_at"
						clean_owners "$connections"

						if ((return_code != 3)); then

							# get orgs of connections
							phase_started_at=$(startup_phase_started_at)
							while read -r connection; do
								[ -n "$connection" ] || continue
								curl_orgs "$connection" >>"$temp_connections"
								phase_status=$?
								if ((phase_status == 3)); then
									return_code=3
									break
								fi
							done <"$connections"
							cat "$temp_connections" >>"$connections"
							clean_owners "$connections"
							log_startup_phase "expand-connection-orgs" "$phase_started_at"
						fi
					fi

					clean_owners "$connections"
					if ! daily_gate_completed_today BKG_LAST_EXPLORE_DATE "$today" && ((return_code != 3)); then
						mark_daily_gate_completed BKG_LAST_EXPLORE_DATE "$today"
					fi
					# shellcheck disable=SC2319
					BKG_PAGE_ALL=$(
						(($(wc -l <"$BKG_OWNERS") < $(($(sort -u "$connections" | wc -l) + 100))))
						echo "$?"
					)
					if ((return_code != 3)); then
						phase_started_at=$(startup_phase_started_at)
						run_owner_page_discovery
						phase_status=$?
						((phase_status != 3)) || return_code=3
						log_startup_phase "page-owner-discovery" "$phase_started_at"
					fi
				else
					phase_started_at=$(startup_phase_started_at)
					get_membership "$GITHUB_OWNER" >"$connections"
					phase_status=$?
					((phase_status != 3)) || return_code=3
					[ "$BKG_IS_FIRST" = "false" ] || : >"$BKG_OWNERS"
					[ "$BKG_IS_FIRST" = "false" ] || : >"$BKG_OPTOUT"
					log_startup_phase "discover-membership" "$phase_started_at"
				fi

				if ((return_code == 3)); then
					echo "Reached BKG_MAX_LEN, stopping after persisting state..."
				else
				if batch_should_reset "$pkg_left"; then
					# reset the batch
					BKG_BATCH_FIRST_STARTED=$today
					set_BKG BKG_BATCH_FIRST_STARTED "$today"
					set_BKG BKG_BATCH_MARKER "$(generate_batch_marker)"
					rm -f packages_to_update
					\cp packages_all packages_to_update
					: >packages_already_updated
				fi

				awk -F'|' '{print $2}' packages_already_updated | awk '!seen[$0]++' >owners_updated
				awk -F'|' '{print $2}' packages_to_update | awk '!seen[$0]++' >all_owners_tu
				grep -Fxf owners_updated all_owners_tu >owners_partially_updated
				grep -vFxf owners_updated all_owners_tu >owners_stale
				bkg_python database deferred-owners "$(date -u +%s)" >owners_deferred || return $?
				while IFS=$'\t' read -r deferred_owner retry_after; do
					[ -n "$deferred_owner" ] || continue
					echo "Deferred $deferred_owner until $(date -u -d "@$retry_after" +%Y-%m-%dT%H:%M:%SZ)"
				done <owners_deferred
				sort "$connections" | uniq -c | sort -nr | awk '{print $2}' >"$connections".bak
				mv "$connections".bak "$connections"
				batch_first_started_sql=$(sqlite_quote_literal "$BKG_BATCH_FIRST_STARTED")
				sqlite3 "$BKG_INDEX_DB" "select owner from $owners_table_sql where date >= $batch_first_started_sql order by owner asc;" >owners_scanned_without_packages
				grep -vFxf owners_scanned_without_packages "$connections" >"$connections".filtered || :
				mv "$connections".filtered "$connections"
				clean_owners "$BKG_OWNERS"
				grep -vFxf all_owners_in_db "$BKG_OWNERS" >owners.tmp
				mv owners.tmp "$BKG_OWNERS"
				rest_first=$(get_BKG BKG_REST_TO_TOP)
				log_prequeue_elapsed_once
				phase_started_at=$(startup_phase_started_at)
				owners_queue_source="$BKG_OWNERS"
				if daily_gate_should_skip_today BKG_LAST_OWNERS_QUEUE_DATE "$today"; then
					owners_queue_source=/dev/null
					echo "Skipping owners.txt queue; already ran today"
				fi
				local owner_candidates_file
				owner_candidates_file=$(mktemp) || return 1
				local owner_ids_file
				owner_ids_file=$(mktemp) || {
					rm -f "$owner_candidates_file"
					return 1
				}
				local missing_owners_file
				missing_owners_file=$(mktemp) || {
					rm -f "$owner_candidates_file" "$owner_ids_file"
					return 1
				}
				local owner_reasons_file
				owner_reasons_file=$(mktemp) || {
					rm -f "$owner_candidates_file" "$owner_ids_file" "$missing_owners_file"
					return 1
				}
				export BKG_OWNER_QUEUE_REASONS_FILE=$owner_reasons_file
				# BKG_INDEX_DIR is initialized by the update.sh entrypoint.
				# shellcheck disable=SC2153
				bash lib/get.sh "$rest_first" "$connections" $request_limit "$GITHUB_OWNER" "$owners_queue_source" "$BKG_INDEX_DIR" "$owner_reasons_file" >"$owner_candidates_file"
				phase_status=$?
				((phase_status != 3)) || return_code=3
				if ((return_code != 3)); then
					if [ -s "$owner_candidates_file" ]; then
						resolve_owner_ids "$owner_candidates_file" "$missing_owners_file" >"$owner_ids_file"
					else
						: >"$owner_ids_file"
					fi
					phase_status=$?
					((phase_status != 3)) || return_code=3
				fi
				if ((return_code != 3)); then
					set_BKG BKG_DISCOVERED_CONNECTION_OWNERS ""
					if [ -s "$owner_ids_file" ]; then
						while IFS= read -r owner_ref; do
							[ -n "$owner_ref" ] || continue
							set_BKG_set BKG_DISCOVERED_CONNECTION_OWNERS "$owner_ref" >/dev/null
						done < <(awk -F'/' 'NR==FNR { discovered[$0] = 1; next } { owner = $NF; if (owner in discovered) print $0 }' "$connections" "$owner_ids_file")
					fi
					[ ! -s "$owner_ids_file" ] || parallel_shell_func "$BKG_ROOT/src/lib/owner.sh" queue_owner_id --lb <"$owner_ids_file"
					phase_status=$?
					((phase_status != 3)) || return_code=3
				fi
				if ((return_code != 3)) && [ -s "$missing_owners_file" ]; then
					while IFS= read -r owner_name; do
						[ -n "$owner_name" ] || continue
						retire_missing_owner "$owner_name" || {
							phase_status=$?
							((phase_status != 3)) || return_code=3
							((phase_status == 3)) || return "$phase_status"
							break
						}
					done < <(sort -u "$missing_owners_file")
				fi
				rm -f "$owner_candidates_file"
				rm -f "$owner_ids_file"
				rm -f "$missing_owners_file"
				rm -f "$owner_reasons_file"
				unset BKG_OWNER_QUEUE_REASONS_FILE
				if [ "$owners_queue_source" != "/dev/null" ] && ((return_code != 3)); then
					mark_daily_gate_completed BKG_LAST_OWNERS_QUEUE_DATE "$today"
				fi
				log_startup_phase "queue-discovered-owners" "$phase_started_at"
				rm -f all_owners_in_db all_owners_tu owners_updated owners_partially_updated owners_stale owners_deferred owners_scanned_without_packages
				set_BKG BKG_DIFF "$db_size_curr"
				set_BKG BKG_REST_TO_TOP "$((1 - rest_first))"
				fi
			fi
		else
			log_prequeue_elapsed_once
			save_owner "$GITHUB_OWNER"
			phase_started_at=$(startup_phase_started_at)
			get_membership "$GITHUB_OWNER" >"$connections"
			if [ -s "$connections" ]; then
				parallel_shell_func "$BKG_ROOT/src/lib/owner.sh" save_owner --lb <"$connections" || while read -r connection; do save_owner "$connection"; done <"$connections"
			fi
			log_startup_phase "queue-membership-owners" "$phase_started_at"
		fi

		rm -f "$connections"
		rm -f "$temp_connections"
		BKG_BATCH_FIRST_STARTED=$(get_BKG BKG_BATCH_FIRST_STARTED)
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

		set_BKG BKG_OUT "$(wc -l <"$BKG_OPTOUT")"
		sqlite3 "$BKG_INDEX_DB" "select owner_id, owner, repo, package from $packages_table_sql;" | sort -u >packages_all
		echo "Preparing the database snapshot..."
		checkpoint_database_for_archive

		# rotate the database if it's greater than 2GB
		if [ "$(stat -c %s "$BKG_INDEX_DB" 2>/dev/null || echo 0)" -ge 2000000000 ]; then
			rotated=true
			echo "Rotating the database..."
			rotate_database_snapshot_if_needed 2000000000 "$BKG_BATCH_FIRST_STARTED"
			echo "Rotated the database"
		fi

		if prepare_database_snapshot_for_archive; then
			echo "Prepared the database snapshot"
		else
			echo "Failed to prepare the database snapshot!"
		fi
	fi

	echo "Hydrating templates and cleaning up..."
	cleanup_generated_json_sidecars "$BKG_INDEX_DIR"
	[ ! -f "$BKG_ROOT"/CHANGELOG.md ] || rm -f "$BKG_ROOT"/CHANGELOG.md
	\cp templates/.CHANGELOG.md "$BKG_ROOT"/CHANGELOG.md
	owners=$(awk -F'|' '{print $1}' packages_all | sort -u | wc -l)
	repos=$(awk -F'|' '{print $1"|"$3}' packages_all | sort -u | wc -l)
	packages=$(wc -l <packages_all)
	sed -i 's/\[DATE\]/'"$(date -u +%F)"'/g; s/\[OWNERS\]/'"$owners"'/g; s/\[REPOS\]/'"$repos"'/g; s/\[PACKAGES\]/'"$packages"'/g' "$BKG_ROOT"/CHANGELOG.md
	! $rotated || echo "P.S. The database was rotated, but you can find all previous data under the [latest release](https://github.com/$GITHUB_OWNER/$GITHUB_REPO/releases/latest)." >>"$BKG_ROOT"/CHANGELOG.md
	[ ! -f "$BKG_ROOT"/README.md ] || rm -f "$BKG_ROOT"/README.md
	\cp templates/.README.md "$BKG_ROOT"/README.md
	sed -i 's/<GITHUB_OWNER>/'"$GITHUB_OWNER"'/g; s/<GITHUB_REPO>/'"$GITHUB_REPO"'/g; s/<GITHUB_BRANCH>/'"$GITHUB_BRANCH"'/g; s/\[PACKAGES\]/'"$packages"'/g; s/\[DATE\]/'"$today"'/g' "$BKG_ROOT"/README.md
		sed -i '/^BKG_VERSIONS_.*=/d; /^BKG_PACKAGES_.*=/d; /^BKG_OWNERS_.*=/d; /^BKG_PAGE_[0-9].*=/d; /^BKG_OWNER_SCAN_.*=/d; /^BKG_TIMEOUT=/d' "$BKG_ENV"
	\cp "$BKG_ROOT"/README.md "$BKG_INDEX_DIR"/README.md
	# shellcheck disable=SC2016
	sed -i 's/src\/img\/logo-b.webp/logo-b.webp/g; s/```py/```prolog/g; s/```js/```jboss-cli/g' "$BKG_INDEX_DIR"/README.md
	\cp img/logo-b.webp "$BKG_INDEX_DIR"/logo-b.webp
	\cp img/logo.ico "$BKG_INDEX_DIR"/favicon.ico
	\cp templates/.index.html "$BKG_INDEX_DIR"/index.html
	\cp templates/fxp.min.js "$BKG_INDEX_DIR"/fxp.min.js
	sed -i 's/GITHUB_REPO/'"$GITHUB_REPO"'/g' "$BKG_INDEX_DIR"/index.html
	rm -f packages_already_updated packages_all packages_to_update
	echo "{
        \"owners\":\"$(numfmt <<<"$owners")\",
        \"repos\":\"$(numfmt <<<"$repos")\",
        \"packages\":\"$(numfmt <<<"$packages")\",
        \"raw_owners\":$owners,
        \"raw_repos\":$repos,
        \"raw_packages\":$packages,
        \"date\":\"$today\"
    }" | tr -d '\n' | jq -c . >"$BKG_INDEX_DIR"/.json
	post_stop_ytox "$BKG_INDEX_DIR"/.json || return $?
	echo "Done!"
	return $return_code
}
