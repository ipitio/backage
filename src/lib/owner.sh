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

owner_scan_clear_legacy_state() {
	[ -n "${owner_id:-}" ] || return 0
	del_BKG BKG_PAGE_"$owner_id"
	del_BKG BKG_OWNER_SCAN_"$owner_id"
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

	local batch_marker=""
	local batch_first_started=""
	local first_page_empty=false
	local next_page=0
	local outcome=""
	local result_file=""
	local update_result=""
	local update_status=0
	batch_first_started=$(current_batch_first_started)
	[ -n "$batch_first_started" ] || batch_first_started="0000-00-00"
	batch_marker=$(get_BKG BKG_BATCH_MARKER)
	[ -n "$batch_marker" ] || return 1
	result_file=$(mktemp) || return 1
	bkg_python owner update \
		"$owner_id" "$owner" "$batch_first_started" \
		"$batch_marker" "${fast_out:-false}" "$result_file" || update_status=$?
	if ((update_status == 0)); then
		update_result=$(<"$result_file")
	fi
	rm -f "$result_file"
	((update_status == 0)) || return "$update_status"
	outcome=$(jq -r '.outcome' <<<"$update_result") || return 1
	first_page_empty=$(jq -r '.first_page_empty' <<<"$update_result") || return 1
	next_page=$(jq -r '.next_page' <<<"$update_result") || return 1

	if $first_page_empty; then
		sed -i '/^\(.*\/\)*'"$owner"'$/d' "$BKG_OWNERS"
	fi
	case "$outcome" in
	missing)
		retire_missing_owner "$owner_id/$owner" || return $?
		return 0
		;;
	paused)
		echo "Paused $owner owner scan at page $next_page"
		return 0
		;;
	deferred)
		return 0
		;;
	updated) ;;
	*)
		echo "Unknown owner update outcome for $owner: $outcome" >&2
		return 1
		;;
	esac

	sed -i '/^\(.*\/\)*'"$owner"'$/d' "$BKG_OWNERS"
	echo "Updated $owner"
}
