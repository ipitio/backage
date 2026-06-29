#!/bin/bash
# shellcheck disable=SC1091,SC2015

source lib/package.sh

save_owner() {
	[ -n "$1" ] || return
	local owner_id
	local status=0
	owner_id=$(resolve_owner_id "$1") || {
		status=$?
		((status != BKG_OWNER_NOT_FOUND_STATUS)) || return 0
		return "$status"
	}
	queue_owner_id "$owner_id"
}

resolve_owner_id() {
	[ -n "$1" ] || return
	local owner_name
	local cached_ref
	local resolved_ref

	if [[ "$1" =~ ^[1-9][0-9]*/.+$ ]]; then
		cache_owner_ref "$1"
		printf '%s\n' "$1"
		return 0
	fi

	owner_name=${1#*/}
	cached_ref=$(lookup_owner_ref_cache "$owner_name")
	if [ -n "$cached_ref" ]; then
		printf '%s\n' "$cached_ref"
		return 0
	fi

	resolved_ref=$(owner_get_id "$1") || return $?
	cache_owner_ref "$resolved_ref"
	printf '%s\n' "$resolved_ref"
}

queue_owner_id() {
	[ -n "$1" ] || return
	local owner_name
	local reason=""
	owner_name=$(cut -d'/' -f2- <<<"$1")
	if [ -f "${BKG_OWNER_QUEUE_REASONS_FILE:-}" ]; then
		reason=$(awk -F'\t' -v owner_key="$owner_name" 'tolower($1) == tolower(owner_key) { print $2; exit }' "$BKG_OWNER_QUEUE_REASONS_FILE")
	fi
	! set_BKG_set BKG_OWNERS_QUEUE "$1" || {
		if [ -n "$reason" ]; then
			echo "Queued $owner_name (reason: $reason)"
		else
			echo "Queued $owner_name"
		fi
	}
}

owner_is_discovered_connection() {
	[ -n "${owner_id:-}" ] || return 1
	[ -n "${owner:-}" ] || return 1
	awk -F'/' -v owner_id_key="$owner_id" -v owner_key="$owner" '$1 == owner_id_key && $2 == owner_key { found = 1; exit } END { exit !found }' < <(get_BKG_set BKG_DISCOVERED_CONNECTION_OWNERS)
}

remember_scanned_owner_without_packages() {
	local batch_first_started=""
	local owners_table_sql

	[ -n "${owner_id:-}" ] || return 0
	[ -n "${owner:-}" ] || return 0
	batch_first_started=$(current_batch_first_started)
	[ -n "$batch_first_started" ] || return 0
	owner_is_discovered_connection || return 0

	sqlite_ensure_index_schema >/dev/null || return $?
	owners_table_sql=$(sqlite_quote_identifier "$BKG_INDEX_TBL_OWN")
	sqlite3 "$BKG_INDEX_DB" "insert or replace into $owners_table_sql (owner_id, owner, date) values ($(sqlite_quote_literal "$owner_id"), $(sqlite_quote_literal "$owner"), $(sqlite_quote_literal "$batch_first_started"));" >/dev/null
}

owner_scan_clear_legacy_state() {
	[ -n "${owner_id:-}" ] || return 0
	del_BKG BKG_PAGE_"$owner_id"
	del_BKG BKG_OWNER_SCAN_"$owner_id"
	OWNER_SCAN_MARKER=""
}

owner_scan_load_active() {
	[ -n "${owner_id:-}" ] || return 1
	local batch_marker
	local result
	batch_marker=$(get_BKG BKG_BATCH_MARKER)
	[ -n "$batch_marker" ] || return 1
	result=$(bkg_python package active-scan "$owner_id" "$batch_marker") || return $?
	OWNER_SCAN_START_PAGE=$(jq -r '.next_page // empty' <<<"$result") || return 1
	if [ "$(jq -r '.discarded_legacy' <<<"$result")" = true ]; then
		echo "Discarding stale owner scan marker for $owner; database state is authoritative"
	fi
}

owner_scan_begin() {
	[ -n "${owner_id:-}" ] || return 1
	[ -n "${owner:-}" ] || return 1
	local batch_marker
	local result
	local started_at

	batch_marker=$(get_BKG BKG_BATCH_MARKER)
	[ -n "$batch_marker" ] || return 1
	started_at=$(date -u +%s)
	result=$(bkg_python package begin-scan \
		"$owner_id" "$owner" "$batch_marker" "$started_at") || return $?
	if [ "$(jq -r '.discarded_legacy' <<<"$result")" = true ]; then
		echo "Discarding stale owner scan marker for $owner; database state is authoritative"
	fi
	OWNER_SCAN_MARKER=$(jq -r '.marker' <<<"$result") || return 1
	start_page=$(jq -r '.next_page' <<<"$result") || return 1
	[ -n "$OWNER_SCAN_MARKER" ] || return 1
	[[ "$start_page" =~ ^[1-9][0-9]*$ ]] || return 1
}

owner_scan_fail() {
	[ -n "${owner_id:-}" ] || return 1
	[ -n "${owner:-}" ] || return 1
	local error=${1:-owner scan failed}
	local marker=${OWNER_SCAN_MARKER:--}
	local retry_after

	retry_after=$(bkg_python database fail-owner-scan \
		"$owner_id" "$owner" "$marker" "$error" "$(date -u +%s)") || return $?
	owner_scan_clear_legacy_state
	echo "Deferred $owner after failed work ($error) until $(date -u -d "@$retry_after" +%Y-%m-%dT%H:%M:%SZ)"
}

owner_refresh_backoff_clear() {
	bkg_python database clear-owner-backoff \
		"$owner_id" "$owner" "$(date -u +%s)"
}

owner_refresh_packages() {
	[ -n "$1" ] || return 0
	local batch_first_started
	batch_first_started=$(current_batch_first_started)
	[ -n "$batch_first_started" ] || batch_first_started="0000-00-00"
	bkg_python owner refresh-packages \
		"$owner_id" "$owner_type" "$owner" "$batch_first_started" \
		"${fast_out:-false}" <<<"$1"
}

OWNER_SCAN_PAGES_RESULT=""

owner_scan_pages() {
	[ -n "$1" ] || return 1
	local result_file
	local status=0
	result_file=$(mktemp) || return 1
	OWNER_SCAN_PAGES_RESULT=""

	bkg_python owner scan-pages \
		"$owner_id" "$owner_type" "$owner" "$OWNER_SCAN_MARKER" \
		"$(current_batch_first_started)" "$1" "${fast_out:-false}" \
		"$result_file" || status=$?
	if ((status == 0)); then
		OWNER_SCAN_PAGES_RESULT=$(<"$result_file")
	fi
	rm -f "$result_file"
	return "$status"
}

owner_scan_verify_missing_packages() {
	[ -n "${OWNER_SCAN_MARKER:-}" ] || return 1
	local change_count
	local refresh_refs
	local result
	local status=0

	result=$(bkg_python owner verify-scan \
		"$owner_id" "$owner" "$OWNER_SCAN_MARKER" \
		"$(current_batch_first_started)" "$(date -u +%s)") || return $?
	change_count=$(jq -r '.identity_changes | length' <<<"$result") || return 1
	if ((change_count > 0)); then
		echo "Reconciled $change_count package repository association(s) for $owner"
	fi
	refresh_refs=$(jq -r \
		'.packages[] | [.package_type, .repo, .package] | join("/")' \
		<<<"$result") || return 1
	if [ -n "$refresh_refs" ]; then
		owner_refresh_packages "$refresh_refs"
		status=$?
	fi

	return "$status"
}

owner_scan_remove_reconciled_files() {
	[ -n "$1" ] || return 0
	local package
	local repo

	while IFS=$'\t' read -r repo package; do
		[ -n "$repo" ] || continue
		[ -n "$package" ] || continue
		rm -f -- "$BKG_INDEX_DIR/$owner/$repo/$package".json*
		rm -f -- "$BKG_INDEX_DIR/$owner/$repo/$package".xml*
		if ! find "$BKG_INDEX_DIR/$owner/$repo" -maxdepth 1 -type f -name '*.json' ! -name '.*' -print -quit 2>/dev/null | grep -q .; then
			rm -rf -- "${BKG_INDEX_DIR:?}/${owner:?}/${repo:?}"
		fi
	done <<<"$1"
}

owner_scan_complete() {
	[ -n "${OWNER_SCAN_MARKER:-}" ] || return 1
	local pending_count
	local pending_summary
	local reconciled
	local result
	local retry_after

	result=$(bkg_python database complete-owner-scan \
		"$owner_id" "$OWNER_SCAN_MARKER" "$(current_batch_first_started)" "$(date -u +%s)") || return $?
	reconciled=$(jq -r '.removed[]? | [.repo, .package] | @tsv' <<<"$result")
	owner_scan_remove_reconciled_files "$reconciled"
	pending_count=$(jq -r '.pending_count' <<<"$result")
	pending_summary=$(jq -r \
		'[.pending[:10][] | (.repo + "/" + .package)] | join(", ")' \
		<<<"$result")
	retry_after=$(jq -r '.retry_after' <<<"$result")
	owner_scan_clear_legacy_state
	if ((pending_count > 0)); then
		((pending_count <= 10)) || pending_summary+=", ..."
		echo "Deferred $owner with $pending_count incomplete package refresh(es) ($pending_summary) until $(date -u -d "@$retry_after" +%Y-%m-%dT%H:%M:%SZ)"
	fi
}

owner_scan_apply_result() {
	[ -n "$1" ] || return 1
	local pending_count
	local pending_summary
	local reconciliation
	local reconciled
	local retry_after

	reconciliation=$(jq -c '.reconciliation // empty' <<<"$1") || return 1
	[ -n "$reconciliation" ] || return 1
	reconciled=$(jq -r '.removed[]? | [.repo, .package] | @tsv' \
		<<<"$reconciliation") || return 1
	owner_scan_remove_reconciled_files "$reconciled"
	pending_count=$(jq -r '.pending_count' <<<"$reconciliation") || return 1
	pending_summary=$(jq -r \
		'[.pending[:10][] | (.repo + "/" + .package)] | join(", ")' \
		<<<"$reconciliation") || return 1
	retry_after=$(jq -r '.retry_after' <<<"$reconciliation") || return 1
	owner_scan_clear_legacy_state
	if ((pending_count > 0)); then
		((pending_count <= 10)) || pending_summary+=", ..."
		echo "Deferred $owner with $pending_count incomplete package refresh(es) ($pending_summary) until $(date -u -d "@$retry_after" +%Y-%m-%dT%H:%M:%SZ)"
	fi
}

graphql_owner_lookup_query() {
	[ -n "$1" ] || return
	local alias_index=0
	local owner
	local query='query {'

	while IFS= read -r owner; do
		[ -n "$owner" ] || continue
		query+=" o${alias_index}: repositoryOwner(login:\"${owner}\") { login ... on User { databaseId } ... on Organization { databaseId } }"
		((alias_index++))
	done <"$1"

	query+=' }'
	printf '%s\n' "$query"
}

resolve_owner_ids() {
	[ -n "$1" ] || return 0
	[ -s "$1" ] || return 0
	local missing_file=${2:-}
	local -a args=(discovery resolve-owner-ids "$1")

	[ -z "$missing_file" ] || args+=("$missing_file")
	bkg_python "${args[@]}"
}

retire_missing_owner() {
	[ -n "$1" ] || return 0
	local owner_name=${1#*/}
	local temp_file

	[[ "$owner_name" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}$ ]] || {
		echo "Refusing to retire invalid owner name: $owner_name" >&2
		return 1
	}

	bkg_python database retire-owner "$owner_name" || return $?
	if index_worktree_is_git_repo; then
		git -C "$BKG_INDEX_DIR" rm -r --sparse --ignore-unmatch -- "$owner_name" >/dev/null || return 1
	fi
	rm -rf -- "${BKG_INDEX_DIR:?}/$owner_name"

	temp_file=$(mktemp) || return 1
	awk -F'/' -v owner_key="$owner_name" '$NF != owner_key' "$BKG_OWNERS" >"$temp_file"
	mv "$temp_file" "$BKG_OWNERS"
	echo "Retired unavailable owner $owner_name"
}

page_owner() {
	[ -n "$1" ] || return
	local per_page=100
	local owner_page_output=""
	local has_more=false
	local key
	local value
	local status=0

	[ -n "$GITHUB_TOKEN" ] || return 2

	echo "Checking owners page $1..."
	((BKG_PAGE_ALL > 0)) && per_page=1 || per_page=100
	check_limit || return $?
	owner_page_output=$(bkg_python discovery admit-owner-page "$1" "$per_page" packages_all) || status=$?
	((status != 3)) || return 3
	((status == 0)) || return "$status"

	while IFS=$'\t' read -r key value; do
		case "$key" in
		has_more)
			has_more=$value
			;;
		requested)
			echo "Requested $value"
			;;
		esac
	done <<<"$owner_page_output"

	echo "Checked owners page $1"
	[ "$has_more" = true ] || return 2
}

update_owner() {
	check_limit || return $?
	[ -n "$1" ] || return
	owner_id=$(cut -d'/' -f1 <<<"$1")
	owner=$(cut -d'/' -f2 <<<"$1")

	if grep -q "^$owner$" "$BKG_OPTOUT"; then
		echo "$owner was opted out!"
		rm -rf "$BKG_INDEX_DIR/${owner:?}"
		bkg_python database retire-owner "$owner" || return $?
		owner_scan_clear_legacy_state
		return
	fi

	echo "Updating $owner..."

	# decode percent-encoded characters and make lowercase (eg. for docker manifest)
	# shellcheck disable=SC2034
	lower_owner=$(perl -pe 's/%([0-9A-Fa-f]{2})/chr(hex($1))/eg' <<<"${owner//%/%25}" | tr '[:upper:]' '[:lower:]')
	(($? != 3)) || return 3
	[ -n "$(grep -zoP 'href="/orgs/'"$owner"'/people"' <<<"$(curl "https://github.com/orgs/$owner/people")" | tr -d '\0')" ] && export owner_type="orgs" || export owner_type="users"
	[ -d "$BKG_INDEX_DIR/$owner" ] || mkdir "$BKG_INDEX_DIR/$owner"
	local start_page
	local batch_first_started=""
	local owner_scan_required=true
	local owner_partially_updated=false
	local pending_count=0
	local refresh_plan=""
	local refresh_refs=""
	local scan_result=""
	local scan_completed=false
	local scan_status=0
	local next_page=""
	start_page=""
	batch_first_started=$(current_batch_first_started)
	[ -n "$batch_first_started" ] || batch_first_started="0000-00-00"

	refresh_plan=$(bkg_python owner refresh-plan \
		"$owner_id" "$owner" "$batch_first_started") || return $?
	owner_partially_updated=$(jq -r '.partially_updated' <<<"$refresh_plan") || return 1
	if $owner_partially_updated; then
		OWNER_SCAN_START_PAGE=""
		owner_scan_load_active || return $?
		start_page=$OWNER_SCAN_START_PAGE
	fi

	if $owner_partially_updated && [ -z "$start_page" ]; then
		refresh_refs=$(jq -r \
			'.packages[] | [.package_type, .repo, .package] | join("/")' \
			<<<"$refresh_plan") || return 1
		owner_refresh_packages "$refresh_refs"
		(($? != 3)) || return 3
		refresh_plan=$(bkg_python owner refresh-plan \
			"$owner_id" "$owner" "$batch_first_started") || return $?
		pending_count=$(jq -r '.pending_count' <<<"$refresh_plan") || return 1
		if ((pending_count > 0)); then
			echo "$owner has $pending_count unresolved package refresh(es); verifying the complete owner listing"
		else
			owner_refresh_backoff_clear || return $?
			owner_scan_required=false
		fi
	fi

	if $owner_scan_required; then
		owner_scan_begin || return $?

		owner_scan_pages "$start_page"
		scan_status=$?
		if ((scan_status == 3)); then
			return 3
		elif ((scan_status != 0)); then
			owner_scan_fail "owner scan or reconciliation pass failed" || return $?
			return 0
		fi
		scan_result=$OWNER_SCAN_PAGES_RESULT
		scan_completed=$(jq -r '.completed' <<<"$scan_result") || return 1
		next_page=$(jq -r '.next_page' <<<"$scan_result") || return 1
		if [ "$(jq -r '.first_page_empty' <<<"$scan_result")" = true ]; then
			sed -i '/^\(.*\/\)*'"$owner"'$/d' "$BKG_OWNERS"
		fi
		if [ "$(jq -r '.owner_missing' <<<"$scan_result")" = true ]; then
			retire_missing_owner "$owner_id/$owner" || return $?
			owner_scan_clear_legacy_state
			return 0
		fi

		if ! $scan_completed; then
			echo "Paused $owner owner scan at page $next_page"
			return 0
		fi

		owner_scan_apply_result "$scan_result" || return $?
	fi

	local owner_publication
	local package_count
	cleanup_generated_json_sidecars "$BKG_INDEX_DIR/$owner"
	echo "Creating $owner arrays..."
	check_script_timeout || return $?
	stop_requested && return 3
	owner_publication=$(bkg_python owner publish "$owner_id" "$owner") || return $?
	package_count=$(jq -r '.package_count' <<<"$owner_publication") || return 1
	if ((package_count == 0)); then
		remember_scanned_owner_without_packages || return $?
	fi

	sed -i '/^\(.*\/\)*'"$owner"'$/d' "$BKG_OWNERS"
	echo "Updated $owner"
}
