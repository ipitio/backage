#!/bin/bash
# shellcheck disable=SC1091,SC2015

source lib/package.sh

request_owner() {
	[ -n "$1" ] || return
	local owner=""
	local id=""
	local return_code=0
	local paging=true
	owner=$(_jq "$1" '.login' 2>/dev/null)
	[ -n "$owner" ] && id=$(_jq "$1" '.id' 2>/dev/null) || paging=false

	if [ -z "$id" ]; then
		owner=$(owner_get_id "$1")
		id=$(cut -d'/' -f1 <<<"$owner")
		owner=$(cut -d'/' -f2 <<<"$owner")
	fi

	cache_owner_ref "$id/$owner"

	! awk -F'|' -v owner_key="$owner" '$2 == owner_key { found = 1; exit } END { exit !found }' packages_all || return 1
	until ln "$BKG_OWNERS" "$BKG_OWNERS.lock" 2>/dev/null; do sleep 0.05; done
	awk -F'/' -v owner_key="$owner" '$NF == owner_key { found = 1; exit } END { exit !found }' "$BKG_OWNERS" || echo "$id/$owner" >>"$BKG_OWNERS"

	if [ "$(stat -c %s "$BKG_OWNERS")" -ge 100000000 ]; then
		sed -i '$d' "$BKG_OWNERS"
		return_code=2
	elif $paging && [ -n "$id" ]; then
		echo "Requested $owner"
		local last_id
		last_id=$(get_BKG BKG_LAST_SCANNED_ID)
		((id <= last_id)) || set_BKG BKG_LAST_SCANNED_ID "$id"
	fi

	rm -f "$BKG_OWNERS.lock"
	return $return_code
}

save_owner() {
	[ -n "$1" ] || return
	local owner_id
	owner_id=$(resolve_owner_id "$1") || return
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
	! set_BKG_set BKG_OWNERS_QUEUE "$1" || echo "Queued $(cut -d'/' -f2 <<<"$1")"
}

owner_is_discovered_connection() {
	[ -n "${owner_id:-}" ] || return 1
	[ -n "${owner:-}" ] || return 1
	awk -F'/' -v owner_id_key="$owner_id" -v owner_key="$owner" '$1 == owner_id_key && $2 == owner_key { found = 1; exit } END { exit !found }' < <(get_BKG_set BKG_DISCOVERED_CONNECTION_OWNERS)
}

remember_scanned_owner_without_packages() {
	[ -n "${owner_id:-}" ] || return 0
	[ -n "${owner:-}" ] || return 0
	[ -n "${BKG_BATCH_FIRST_STARTED:-}" ] || return 0
	owner_is_discovered_connection || return 0

	sqlite3 "$BKG_INDEX_DB" "create table if not exists '$BKG_INDEX_TBL_OWN' (
		owner_id text not null,
		owner text not null,
		date text not null,
		primary key (owner_id, date)
	);" >/dev/null || return $?
	sqlite3 "$BKG_INDEX_DB" "insert or replace into '$BKG_INDEX_TBL_OWN' (owner_id, owner, date) values ('$(sqlite_escape_literal "$owner_id")', '$(sqlite_escape_literal "$owner")', '$(sqlite_escape_literal "$BKG_BATCH_FIRST_STARTED")');" >/dev/null
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
	local candidate
	local owner_name
	local owner_id
	local query
	local response
	local batch_file=""
	local unresolved_file=""
	local resolved=""
	local -a candidates=()
	local -a unresolved=()
	local -A resolved_by_owner=()

	while IFS= read -r candidate; do
		[ -n "$candidate" ] || continue
		candidates+=("$candidate")

		if [[ "$candidate" =~ ^[1-9][0-9]*/.+$ ]]; then
			owner_name=$(cut -d'/' -f2- <<<"$candidate")
			resolved_by_owner["$owner_name"]="$candidate"
			cache_owner_ref "$candidate"
			continue
		fi

		owner_name=${candidate#*/}
		[ -n "$owner_name" ] || continue
		resolved=$(lookup_owner_ref_cache "$owner_name")
		if [ -n "$resolved" ]; then
			resolved_by_owner["$owner_name"]="$resolved"
			continue
		fi
		[[ -n "${resolved_by_owner[$owner_name]:-}" ]] && continue
		unresolved+=("$owner_name")
	done <"$1"

	if ((${#unresolved[@]} > 0)) && [ -n "${GITHUB_TOKEN:-}" ]; then
		unresolved_file=$(mktemp) || return 1
		printf '%s\n' "${unresolved[@]}" | awk '!seen[$0]++' >"$unresolved_file"

		while [ -s "$unresolved_file" ]; do
			batch_file=$(mktemp) || {
				rm -f "$unresolved_file"
				return 1
			}
			head -n 50 "$unresolved_file" >"$batch_file"
			query=$(graphql_owner_lookup_query "$batch_file")
			response=$(query_graphql_api "$query")
			(($? != 3)) || {
				rm -f "$batch_file" "$unresolved_file"
				return 3
			}
			while IFS=$'\t' read -r owner_name owner_id; do
				[ -n "$owner_name" ] || continue
				[ -n "$owner_id" ] || continue
				resolved_by_owner["$owner_name"]="$owner_id/$owner_name"
				cache_owner_ref "$owner_id/$owner_name"
			done < <(jq -r '.data | to_entries[] | select(.value != null and .value.login != null and .value.databaseId != null) | "\(.value.login)\t\(.value.databaseId)"' <<<"$response" 2>/dev/null)
			tail -n +51 "$unresolved_file" >"$unresolved_file.next"
			mv "$unresolved_file.next" "$unresolved_file"
			rm -f "$batch_file"
		done

		rm -f "$unresolved_file"
	fi

	for candidate in "${candidates[@]}"; do
		if [[ "$candidate" =~ ^[1-9][0-9]*/.+$ ]]; then
			printf '%s\n' "$candidate"
			continue
		fi

		owner_name=${candidate#*/}
		resolved=${resolved_by_owner[$owner_name]:-}

		if [ -z "$resolved" ]; then
			resolved=$(owner_get_id "$owner_name")
			(($? != 3)) || return 3
			cache_owner_ref "$resolved"
		fi

		[ -z "$resolved" ] || printf '%s\n' "$resolved"
	done | awk 'NF && $0 !~ /^\// && !seen[$0]++'
}

owner_merge_pages_json() {
	printf '%s\n%s\n' "${1:-[]}" "${2:-[]}" | jq -cs 'add | unique_by(.login)'
}

owner_build_json_array() {
	[ -n "$1" ] || return
	local json_file
	local -a json_files=()

	mapfile -d '' -t json_files < <(find "$1" -type f -name '*.json' ! -name '.*' -print0 | LC_ALL=C sort -z)

	if ((${#json_files[@]} == 0)); then
		printf '[]\n'
		return 0
	fi

	for json_file in "${json_files[@]}"; do
		cat "$json_file"
		printf '\n'
	done | jq -cs '.'
}

page_owner() {
	[ -n "$1" ] || return
	local owners_more="[]"
	local users_more="[]"
	local orgs_more="[]"
	local per_page=100
	local users_count=0
	local orgs_count=0

	if [ -n "$GITHUB_TOKEN" ]; then
		echo "Checking owners page $1..."
		local last_id
		last_id=$(get_BKG BKG_LAST_SCANNED_ID)
		((BKG_PAGE_ALL > 0)) && per_page=1 || per_page=100
		users_more=$(query_api "users?per_page=$per_page&page=$1&since=$last_id")
		orgs_more=$(query_api "organizations?per_page=$per_page&page=$1&since=$last_id")
		users_count=$(jq 'length' <<<"$users_more" 2>/dev/null || echo 0)
		orgs_count=$(jq 'length' <<<"$orgs_more" 2>/dev/null || echo 0)
		owners_more=$(owner_merge_pages_json "$users_more" "$orgs_more")
	fi

	# if owners doesn't have .login, break
	jq -e '.[].login' <<<"$owners_more" &>/dev/null || return 2
	local owners_lines
	owners_lines=$(jq -r '.[] | @base64' <<<"$owners_more")
	run_parallel request_owner "$owners_lines"
	echo "Checked owners page $1"
	((users_count >= per_page || orgs_count >= per_page)) || return 2
}

update_owner() {
	check_limit || return $?
	[ -n "$1" ] || return
	owner_id=$(cut -d'/' -f1 <<<"$1")
	owner=$(cut -d'/' -f2 <<<"$1")

	if grep -q "^$owner$" "$BKG_OPTOUT"; then
		echo "$owner was opted out!"
		rm -rf "$BKG_INDEX_DIR/${owner:?}"
		sqlite3 "$BKG_INDEX_DB" "delete from '$BKG_INDEX_TBL_PKG' where owner_id='$owner_id';"
		sqlite3 "$BKG_INDEX_DB" "select name from sqlite_master where type='table' and name glob '${BKG_INDEX_TBL_VER}_*';" | while IFS= read -r table_name; do
			[ -n "$table_name" ] || continue
			[ "$(cut -d'_' -f4 <<<"$table_name")" = "$owner" ] || continue
			sqlite3 "$BKG_INDEX_DB" "drop table if exists '$table_name';"
		done
		set_BKG BKG_PAGE_"$owner_id" ""
		del_BKG BKG_PAGE_"$owner_id"
		return
	fi

	echo "Updating $owner..."

	# decode percent-encoded characters and make lowercase (eg. for docker manifest)
	# shellcheck disable=SC2034
	lower_owner=$(perl -pe 's/%([0-9A-Fa-f]{2})/chr(hex($1))/eg' <<<"${owner//%/%25}" | tr '[:upper:]' '[:lower:]')
	(($? != 3)) || return 3
	[ -n "$(grep -zoP 'href="/orgs/'"$owner"'/people"' <<<"$(curl "https://github.com/orgs/$owner/people")" | tr -d '\0')" ] && export owner_type="orgs" || export owner_type="users"
	[ -d "$BKG_INDEX_DIR/$owner" ] || mkdir "$BKG_INDEX_DIR/$owner"
	set_BKG BKG_PACKAGES_"$owner" ""
	local start_page
	start_page=$(get_BKG BKG_PAGE_"$owner_id")

	if awk -F'|' -v owner_id_key="$owner_id" -v owner_key="$owner" '$1 == owner_id_key && $2 == owner_key { found = 1; exit } END { exit !found }' packages_already_updated && [ -z "$start_page" ]; then
		run_parallel save_package "$(sqlite3 "$BKG_INDEX_DB" "select package_type, package, max(date) as max_date from '$BKG_INDEX_TBL_PKG' where owner_id = '$owner_id' group by package_type, package having max(date) < '$BKG_BATCH_FIRST_STARTED' order by max_date asc;" | awk -F'|' '{print "////"$1"//"$2}')"
		(($? != 3)) || return 3
		run_parallel update_package "$(get_BKG_set BKG_PACKAGES_"$owner")"
		(($? != 3)) || return 3
	else
		[ -n "$start_page" ] || start_page=1

		for page in $(seq "$start_page" 100000); do
			local pages_left=0
			((page <= start_page + 1)) || set_BKG BKG_PAGE_"$owner_id" "$page"
			((page - start_page < 51)) || break
			page_package "$page"
			pages_left=$?
			run_parallel update_package "$(get_BKG_set BKG_PACKAGES_"$owner")"
			(($? != 3)) || return 3

			if ((pages_left == 2)); then
				set_BKG BKG_PAGE_"$owner_id" ""
				del_BKG BKG_PAGE_"$owner_id"
				break
			fi

			set_BKG BKG_PACKAGES_"$owner" ""
		done
	fi

	local owner_repos
	local owner_has_packages
	cleanup_generated_json_sidecars "$BKG_INDEX_DIR/$owner"
	owner_repos=$(find "$BKG_INDEX_DIR/$owner" -mindepth 1 -maxdepth 1 -type d -print0 | xargs -0 -I {} basename {})
	owner_has_packages=$(sqlite3 "$BKG_INDEX_DB" "select 1 from '$BKG_INDEX_TBL_PKG' where owner_id='$(sqlite_escape_literal "$owner_id")' limit 1;" 2>/dev/null || :)
	if [ -z "$owner_repos" ] && ! [[ "$owner_has_packages" =~ ^1$ ]]; then
		remember_scanned_owner_without_packages || return $?
	fi

	if [ -n "$owner_repos" ]; then
		echo "Creating $owner array..."
		owner_build_json_array "$BKG_INDEX_DIR/$owner" >"$BKG_INDEX_DIR/$owner/.json.tmp"
		mv -f "$BKG_INDEX_DIR/$owner/.json.tmp" "$BKG_INDEX_DIR/$owner/.json"
		bash lib/ytoxt.sh "$BKG_INDEX_DIR/$owner/.json"

		echo "Creating $owner repo arrays..."
		parallel "jq -c --arg repo {} '[.[] | select(.repo == \$repo)]' \"$BKG_INDEX_DIR/$owner/.json\" > \"$BKG_INDEX_DIR/$owner/{}/.json.tmp\"" <<<"$owner_repos"
		xargs -I {} mv -f "$BKG_INDEX_DIR/$owner/{}/.json.tmp" "$BKG_INDEX_DIR/$owner/{}/.json" 2>/dev/null <<<"$owner_repos"
		xargs -I {} bash -c "bash lib/ytoxt.sh \"$BKG_INDEX_DIR/$owner/{}/.json\"" <<<"$owner_repos"
	fi

	sed -i '/^\(.*\/\)*'"$owner"'$/d' "$BKG_OWNERS"
	echo "Updated $owner"
}
