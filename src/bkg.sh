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

		while kill -0 "$updates_pid" 2>/dev/null; do
			sleep 30
			kill -0 "$updates_pid" 2>/dev/null || break
			[ "$(get_BKG BKG_TIMEOUT)" = "1" ] || continue
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

db_restore_signature_file() {
	printf '%s\n' "${BKG_INDEX_DB}.snapshot.sha256"
}

current_index_snapshot_archive_file() {
	local db_archive_file=""
	local legacy_archive_file=""

	db_archive_file=$(db_snapshot_archive_file 2>/dev/null || :)
	if [ -n "$db_archive_file" ] && [ -f "$db_archive_file" ]; then
		printf '%s\n' "$db_archive_file"
		return 0
	fi

	legacy_archive_file=$(legacy_sql_snapshot_archive_file 2>/dev/null || :)
	if [ -n "$legacy_archive_file" ] && [ -f "$legacy_archive_file" ]; then
		printf '%s\n' "$legacy_archive_file"
		return 0
	fi

	return 1
}

current_index_snapshot_signature() {
	local archive_file
	archive_file=$(current_index_snapshot_archive_file) || return 1
	sha256sum "$archive_file" | awk '{print $1}'
}

restore_db_from_index_snapshot_if_needed() {
	local archive_file
	local archive_name
	local archive_kind
	local signature_file
	local current_signature
	local stored_signature=""
	local restore_started_at=0
	local db_tmp=""

	archive_file=$(current_index_snapshot_archive_file) || return 0
	archive_name=$(basename "$archive_file")
	case "$archive_file" in
		*.db.zst) archive_kind="db" ;;
		*) archive_kind="sql" ;;
	esac

	signature_file=$(db_restore_signature_file)
	current_signature=$(sha256sum "$archive_file" | awk '{print $1}')
	[ -f "$signature_file" ] && stored_signature=$(cat "$signature_file")

	if [ -s "$BKG_INDEX_DB" ] && [ -n "$stored_signature" ] && [ "$stored_signature" = "$current_signature" ]; then
		echo "Using existing database; $archive_name unchanged"
		return 0
	fi

	restore_started_at=$(startup_phase_started_at)
	[ ! -f "$BKG_INDEX_DB" ] || mv "$BKG_INDEX_DB" "$BKG_INDEX_DB".bak

	if [ "$archive_kind" = "db" ]; then
		echo "Restoring database from $archive_name..."
		db_tmp=$(mktemp "$(dirname "$BKG_INDEX_DB")/.${BKG_INDEX_DB##*/}.XXXXXX") || return 1
		if unzstd -c "$archive_file" >"$db_tmp"; then
			mv -f "$db_tmp" "$BKG_INDEX_DB"
			log_startup_phase "decompress-db-archive" "$restore_started_at"
		else
			rm -f "$db_tmp"
		fi
	else
		echo "Restoring database from legacy $archive_name..."
		if unzstd -c "$archive_file" | command sqlite3 "$BKG_INDEX_DB"; then
			log_startup_phase "import-legacy-sql-archive" "$restore_started_at"
		fi
	fi

	if [ -f "$BKG_INDEX_DB" ]; then
		printf '%s\n' "$current_signature" >"$signature_file"
		[ ! -f "$BKG_INDEX_DB".bak ] || rm -f "$BKG_INDEX_DB".bak
		return 0
	fi

	[ ! -f "$BKG_INDEX_DB" ] || rm -f "$BKG_INDEX_DB"
	[ ! -f "$BKG_INDEX_DB".bak ] || mv "$BKG_INDEX_DB".bak "$BKG_INDEX_DB"
	return 1
}

write_db_restore_signature() {
	local current_signature
	current_signature=$(current_index_snapshot_signature) || return 0
	printf '%s\n' "$current_signature" >"$(db_restore_signature_file)"
}

checkpoint_database_for_archive() {
	command sqlite3 "$BKG_INDEX_DB" 'pragma wal_checkpoint(truncate);' >/dev/null 2>&1 || sqlite3 "$BKG_INDEX_DB" 'pragma wal_checkpoint(truncate);' >/dev/null 2>&1 || :
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
	local rest_first
	local request_limit=200
	local phase_started_at=0
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
	BKG_BATCH_FIRST_STARTED=$(get_BKG BKG_BATCH_FIRST_STARTED)
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
	sqlite3 "$BKG_INDEX_DB" "create table if not exists '$BKG_INDEX_TBL_PKG' (
        owner_id text,
        owner_type text not null,
        package_type text not null,
        owner text not null,
        repo text not null,
        package text not null,
        downloads integer not null,
        downloads_month integer not null,
        downloads_week integer not null,
        downloads_day integer not null,
        size integer not null,
        date text not null,
        primary key (owner_id, package, date)
    ); pragma auto_vacuum = full;"
	sqlite3 "$BKG_INDEX_DB" "select owner_id, owner, repo, package, max(date) as max_date from '$BKG_INDEX_TBL_PKG' group by owner_id, owner, repo, package having max(date) >= '$BKG_BATCH_FIRST_STARTED' order by max_date asc;" >packages_already_updated
	sqlite3 "$BKG_INDEX_DB" "select owner_id, owner, repo, package, max(date) as max_date from '$BKG_INDEX_TBL_PKG' group by owner_id, owner, repo, package order by date asc;" >packages_all
	sqlite3 "$BKG_INDEX_DB" "select owner_id, owner, max(date) as max_date from '$BKG_INDEX_TBL_PKG' group by owner_id, owner order by date asc;" | awk -F'|' '{print $2}' >all_owners_in_db
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
					phase_started_at=$(startup_phase_started_at)
					explore "$GITHUB_OWNER" >"$connections"
					phase_status=$?
					((phase_status != 3)) || return_code=3
					explore "$GITHUB_OWNER/$GITHUB_REPO" >>"$connections"
					phase_status=$?
					((phase_status != 3)) || return_code=3
					log_startup_phase "discover-connections" "$phase_started_at"

					if ((return_code != 3)); then

						# get orgs of connections
						phase_started_at=$(startup_phase_started_at)
						while read -r connection; do
							curl_orgs "$connection" >>"$temp_connections"
							phase_status=$?
							if ((phase_status == 3)); then
								return_code=3
								break
							fi
						done <"$connections"
						cat "$temp_connections" >>"$connections"
						log_startup_phase "expand-connection-orgs" "$phase_started_at"
					fi

					sed -i 's/^[[:space:]]*//;s/[[:space:]]*$//; /^$/d; /^0\/$/d' "$connections"
					# shellcheck disable=SC2319
					BKG_PAGE_ALL=$(
						(($(wc -l <"$BKG_OWNERS") < $(($(sort -u "$connections" | wc -l) + 100))))
						echo "$?"
					)
					if ((return_code != 3)); then
						phase_started_at=$(startup_phase_started_at)
						seq 1 2 | parallel_shell_func "$BKG_ROOT/src/lib/owner.sh" page_owner --lb --halt soon,fail=1
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
				if (( 9999 < pkg_done )) || (( pkg_left < 4 )) || [[ "${db_size_curr::-4}" == "${db_size_prev::-4}" ]]; then
					BKG_BATCH_FIRST_STARTED=$today
					set_BKG BKG_BATCH_FIRST_STARTED "$today"
					rm -f packages_to_update
					\cp packages_all packages_to_update
					: >packages_already_updated
					[ "${db_size_curr::-4}" != "${db_size_prev::-4}" ] || echo "Database size unchanged! Previous: $db_size_prev; Current: $db_size_curr"
				fi

				awk -F'|' '{print $2}' packages_already_updated | awk '!seen[$0]++' >owners_updated
				awk -F'|' '{print $2}' packages_to_update | awk '!seen[$0]++' >all_owners_tu
				grep -Fxf owners_updated all_owners_tu >owners_partially_updated
				grep -vFxf owners_updated all_owners_tu >owners_stale
				sort "$connections" | uniq -c | sort -nr | awk '{print $2}' >"$connections".bak
				mv "$connections".bak "$connections"
				clean_owners "$BKG_OWNERS"
				grep -vFxf all_owners_in_db "$BKG_OWNERS" >owners.tmp
				mv owners.tmp "$BKG_OWNERS"
				rest_first=$(get_BKG BKG_REST_TO_TOP)
				log_prequeue_elapsed_once
				phase_started_at=$(startup_phase_started_at)
				bash lib/get.sh "$rest_first" "$connections" $request_limit "$GITHUB_OWNER" "$BKG_OWNERS" "$BKG_INDEX_DIR" | parallel_shell_func "$BKG_ROOT/src/lib/owner.sh" save_owner --lb
				log_startup_phase "queue-discovered-owners" "$phase_started_at"
				rm -f all_owners_in_db all_owners_tu owners_updated owners_partially_updated owners_stale
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
			run_owner_updates
			phase_status=$?
			if ((phase_status == 3)); then
				return_code=3
				echo "Reached BKG_MAX_LEN, stopping after persisting state..."
			fi
		fi

		set_BKG BKG_OUT "$(wc -l <"$BKG_OPTOUT")"
		sqlite3 "$BKG_INDEX_DB" "select owner_id, owner, repo, package from '$BKG_INDEX_TBL_PKG';" | sort -u >packages_all
		echo "Compressing the database..."
		checkpoint_database_for_archive
		db_archive_file=$(db_snapshot_archive_file)
		db_archive_tmp="$db_archive_file.new"
		zstd -22 --ultra --long -T0 "$BKG_INDEX_DB" -o "$db_archive_tmp"

		if [ -f "$db_archive_tmp" ]; then
			# rotate the database if it's greater than 2GB
			if [ -f "$db_archive_file" ] && [ "$(stat -c %s "$db_archive_tmp")" -ge 2000000000 ]; then
				rotated=true
				echo "Rotating the database..."
				local older_db
				older_db="$BKG_ROOT/$(date -u +%Y.%m.%d).$(basename "$db_archive_file")"
				[ ! -f "$older_db" ] || rm -f "$older_db"
				mv "$db_archive_file" "$older_db"
				sqlite3 "$BKG_INDEX_DB" "delete from '$BKG_INDEX_TBL_PKG' where date < '$BKG_BATCH_FIRST_STARTED';"
				sqlite3 "$BKG_INDEX_DB" "select name from sqlite_master where type='table' and name like '${BKG_INDEX_TBL_VER}_%';" | parallel --lb "sqlite3 '$BKG_INDEX_DB' 'delete from {} where date < \"$BKG_BATCH_FIRST_STARTED\";'"
				sqlite3 "$BKG_INDEX_DB" "vacuum;"
				checkpoint_database_for_archive
				rm -f "$db_archive_tmp"
				zstd -22 --ultra --long -T0 "$BKG_INDEX_DB" -o "$db_archive_tmp"
				echo "Rotated the database"
			fi

			mv "$db_archive_tmp" "$db_archive_file"
			legacy_archive_file=$(legacy_sql_snapshot_archive_file 2>/dev/null || :)
			[ -z "$legacy_archive_file" ] || rm -f "$legacy_archive_file"
			write_db_restore_signature
			chmod 666 "$db_archive_file"
			echo "Compressed the database"
		else
			echo "Failed to compress the database!"
		fi
	fi

	echo "Hydrating templates and cleaning up..."
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
	sed -i '/^BKG_VERSIONS_.*=/d; /^BKG_PACKAGES_.*=/d; /^BKG_OWNERS_.*=/d; /^BKG_TIMEOUT=/d' "$BKG_ENV"
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
	ytox "$BKG_INDEX_DIR"/.json
	echo "Done!"
	return $return_code
}
