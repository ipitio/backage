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

owner_build_json_array_once() {
	[ -n "$1" ] || return
	local json_file
	local package_json
	local status=0
	local version_limit=${2:-${BKG_OWNER_ARRAY_VERSION_LIMIT:--1}}
	local first=true
	local -a json_files=()

	[[ "$version_limit" =~ ^-?[0-9]+$ ]] || version_limit=-1

	mapfile -d '' -t json_files < <(find "$1" -type f -name '*.json' ! -name '.*' -print0 | LC_ALL=C sort -z)

	if ((${#json_files[@]} == 0)); then
		printf '[]\n'
		return 0
	fi

	printf '['
	for json_file in "${json_files[@]}"; do
		script_stop_requested && return 3
		package_json=$(jq -c --argjson version_limit "$version_limit" '
			def id_to_num:
				if type == "number" then .
				elif type == "string" then tonumber? // 0
				else 0 end;
			def limited_versions($limit):
				if $limit < 0 or ((.version? // null) | type) != "array" then
					.
				else
					.version |= (
						if $limit == 0 then
							[.[] | select(.latest == true or .newest == true)]
						else
							(
								[.[] | select(.latest == true or .newest == true)]
								+ (sort_by(.id | id_to_num) | .[-$limit:])
							)
						end
						| unique_by(.id | tostring)
						| sort_by(.id | id_to_num)
					)
				end;
			limited_versions($version_limit)
		' "$json_file")
		status=$?
		((status == 0)) || return "$status"
		[ -n "$package_json" ] || continue
		$first || printf ','
		printf '%s' "$package_json"
		first=false
	done
	printf ']\n'
}

owner_array_target_bytes() {
	local target=${BKG_OWNER_ARRAY_MAX_BYTES:-35000000}

	[[ "$target" =~ ^[1-9][0-9]*$ ]] || target=35000000
	printf '%s\n' "$target"
}

owner_array_source_json_size() {
	[ -n "$1" ] || return

	find "$1" -type f -name '*.json' ! -name '.*' -printf '%s\n' | awk '{s += $1; n++} END {print s + n + 2}'
}

owner_build_json_array_limit_to_file() {
	[ -n "$1" ] || return
	[ -n "$2" ] || return
	[ -n "$3" ] || return

	run_command_to_file_with_stop_check "$2" owner_build_json_array_once "$1" "$3"
}

owner_build_json_array_try_limit() {
	[ -n "$1" ] || return
	[ -n "$2" ] || return
	[ -n "$3" ] || return

	owner_build_json_array_limit_to_file "$1" "$2" "$3" || return $?
	OWNER_ARRAY_LAST_SIZE=$(stat -c %s "$2" 2>/dev/null || echo 0)
}

owner_build_json_array_to_file() {
	[ -n "$1" ] || return
	[ -n "$2" ] || return
	local owner_dir=$1
	local output_file=$2
	local target_bytes
	local source_size
	local tmp_file=""
	local best_file=""
	local current_size=0
	local status=0
	local low=0
	local high=1
	local mid
	local max_probe=${BKG_OWNER_ARRAY_ADAPTIVE_MAX_PROBE:-65536}

	[[ "$max_probe" =~ ^[1-9][0-9]*$ ]] || max_probe=65536

	if [ -n "${BKG_OWNER_ARRAY_VERSION_LIMIT+x}" ]; then
		owner_build_json_array_limit_to_file "$owner_dir" "$output_file" "$BKG_OWNER_ARRAY_VERSION_LIMIT"
		return $?
	fi

	target_bytes=$(owner_array_target_bytes)
	source_size=$(owner_array_source_json_size "$owner_dir")
	if ((source_size <= target_bytes)); then
		owner_build_json_array_limit_to_file "$owner_dir" "$output_file" "-1"
		return $?
	fi

	best_file=$(mktemp "$(dirname "$output_file")/.${output_file##*/}.best.XXXXXX") || return 1
	owner_build_json_array_try_limit "$owner_dir" "$best_file" "0" || {
		status=$?
		rm -f "$best_file"
		return "$status"
	}
	current_size=$OWNER_ARRAY_LAST_SIZE

	if ((current_size > target_bytes)); then
		echo "Aggregate minimum size ${current_size} exceeds target ${target_bytes}; publishing latest/newest slice" >&2
		mv -f "$best_file" "$output_file"
		return 0
	fi

	while ((high <= max_probe)); do
		tmp_file=$(mktemp "$(dirname "$output_file")/.${output_file##*/}.try.XXXXXX") || {
			rm -f "$best_file"
			return 1
		}
		owner_build_json_array_try_limit "$owner_dir" "$tmp_file" "$high" || {
			status=$?
			rm -f "$tmp_file" "$best_file"
			return "$status"
		}
		current_size=$OWNER_ARRAY_LAST_SIZE

		if ((current_size <= target_bytes)); then
			mv -f "$tmp_file" "$best_file"
			low=$high
			((high *= 2))
			continue
		fi

		rm -f "$tmp_file"
		break
	done

	if ((high <= max_probe)); then
		while ((low + 1 < high)); do
			mid=$(((low + high) / 2))
			tmp_file=$(mktemp "$(dirname "$output_file")/.${output_file##*/}.try.XXXXXX") || {
				rm -f "$best_file"
				return 1
			}
			owner_build_json_array_try_limit "$owner_dir" "$tmp_file" "$mid" || {
				status=$?
				rm -f "$tmp_file" "$best_file"
				return "$status"
			}
			current_size=$OWNER_ARRAY_LAST_SIZE

			if ((current_size <= target_bytes)); then
				mv -f "$tmp_file" "$best_file"
				low=$mid
			else
				rm -f "$tmp_file"
				high=$mid
			fi
		done
	fi

	mv -f "$best_file" "$output_file"
}

owner_package_rows_from_db() {
	[ -n "$1" ] || return
	local owner_id_sql
	local repo_filter_sql=""
	local packages_table_sql

	sqlite_ensure_index_schema >/dev/null || return $?
	owner_id_sql=$(sqlite_quote_literal "$1")
	packages_table_sql=$(sqlite_quote_identifier "$BKG_INDEX_TBL_PKG")
	if [ -n "${2:-}" ]; then
		repo_filter_sql="and p.repo = $(sqlite_quote_literal "$2")"
	fi

	sqlite3 "$BKG_INDEX_DB" "
		with latest_dates as (
			select owner_id, package, max(date) as latest_date
			from $packages_table_sql
			where owner_id = $owner_id_sql
			group by owner_id, package
		),
		latest_packages as (
			select
				p.owner_id,
				p.owner_type,
				p.package_type,
				p.owner,
				p.repo,
				p.package,
				p.downloads,
				p.downloads_month,
				p.downloads_week,
				p.downloads_day,
				p.size,
				p.date
			from $packages_table_sql p
			join latest_dates l
			  on p.owner_id = l.owner_id
			 and p.package = l.package
			 and p.date = l.latest_date
			where p.owner_id = $owner_id_sql
		),
		ranked_packages as (
			select
				*,
				rank() over (order by downloads desc) as owner_rank,
				rank() over (partition by repo order by downloads desc) as repo_rank
			from latest_packages
		)
		select
			owner_id,
			owner_type,
			package_type,
			owner,
			repo,
			package,
			downloads,
			downloads_month,
			downloads_week,
			downloads_day,
			size,
			date,
			owner_rank,
			repo_rank
		from ranked_packages p
		where p.owner_id = $owner_id_sql
		  $repo_filter_sql
		order by p.owner, p.repo, p.package_type, p.package;
	"
}

owner_repo_names_from_db() {
	[ -n "$1" ] || return
	local owner_id_sql
	local packages_table_sql

	sqlite_ensure_index_schema >/dev/null || return $?
	owner_id_sql=$(sqlite_quote_literal "$1")
	packages_table_sql=$(sqlite_quote_identifier "$BKG_INDEX_TBL_PKG")
	sqlite3 "$BKG_INDEX_DB" "select distinct repo from $packages_table_sql where owner_id = $owner_id_sql order by repo;"
}

owner_build_json_array_from_db_once() {
	[ -n "$1" ] || return
	local owner_id_filter=$1
	local repo_filter=${2:-}
	local version_limit=${3:-${BKG_OWNER_ARRAY_VERSION_LIMIT:--1}}
	local package_date
	local package_owner_rank
	local package_repo_rank
	local package_json_file
	local first=true
	local status=0
	local row_count=0

	[[ "$version_limit" =~ ^-?[0-9]+$ ]] || version_limit=-1

	printf '['
	while IFS='|' read -r owner_id owner_type package_type owner repo package raw_downloads raw_downloads_month raw_downloads_week raw_downloads_day size package_date package_owner_rank package_repo_rank; do
		[ -n "$owner_id" ] || continue
		script_stop_requested && return 3
		row_count=$((row_count + 1))
		table_version_name="${BKG_INDEX_TBL_VER}_${owner_type}_${package_type}_${owner}_${repo}_${package}"
		package_json_file=$(mktemp) || return 1
		package_render_json_from_db_context "$package_json_file" "$package_date" "$version_limit" "$package_owner_rank" "$package_repo_rank" || status=$?
		if ((status != 0)); then
			rm -f "$package_json_file"
			return "$status"
		fi
		$first || printf ','
		cat "$package_json_file"
		rm -f "$package_json_file"
		first=false
	done < <(owner_package_rows_from_db "$owner_id_filter" "$repo_filter")
	printf ']\n'
	((row_count > 0)) || return 0
}

owner_build_json_array_from_db_limit_to_file() {
	[ -n "$1" ] || return
	[ -n "$3" ] || return
	[ -n "$4" ] || return

	run_command_to_file_with_stop_check "$3" owner_build_json_array_from_db_once "$1" "$2" "$4"
}

owner_build_json_array_from_db_try_limit() {
	[ -n "$1" ] || return
	[ -n "$3" ] || return
	[ -n "$4" ] || return

	owner_build_json_array_from_db_limit_to_file "$1" "$2" "$3" "$4" || return $?
	OWNER_ARRAY_LAST_SIZE=$(stat -c %s "$3" 2>/dev/null || echo 0)
}

owner_build_json_array_from_db_to_file() {
	[ -n "$1" ] || return
	[ -n "$4" ] || return
	local owner_id_filter=$1
	local repo_filter=${2:-}
	local size_hint_dir=$3
	local output_file=$4
	local target_bytes
	local source_size=0
	local source_count=0
	local tmp_file=""
	local best_file=""
	local current_size=0
	local status=0
	local low=0
	local high=1
	local mid
	local max_probe=${BKG_OWNER_ARRAY_ADAPTIVE_MAX_PROBE:-65536}

	[[ "$max_probe" =~ ^[1-9][0-9]*$ ]] || max_probe=65536

	if [ -n "${BKG_OWNER_ARRAY_VERSION_LIMIT+x}" ]; then
		owner_build_json_array_from_db_limit_to_file "$owner_id_filter" "$repo_filter" "$output_file" "$BKG_OWNER_ARRAY_VERSION_LIMIT"
		return $?
	fi

	target_bytes=$(owner_array_target_bytes)
	if [ -d "$size_hint_dir" ]; then
		source_size=$(owner_array_source_json_size "$size_hint_dir")
		source_count=$(find "$size_hint_dir" -type f -name '*.json' ! -name '.*' -printf . | wc -c)
	fi
	if ((source_count > 0 && source_size <= target_bytes)); then
		owner_build_json_array_from_db_limit_to_file "$owner_id_filter" "$repo_filter" "$output_file" "-1"
		return $?
	fi

	best_file=$(mktemp "$(dirname "$output_file")/.${output_file##*/}.best.XXXXXX") || return 1
	owner_build_json_array_from_db_try_limit "$owner_id_filter" "$repo_filter" "$best_file" "0" || {
		status=$?
		rm -f "$best_file"
		return "$status"
	}
	current_size=$OWNER_ARRAY_LAST_SIZE

	if ((current_size > target_bytes)); then
		echo "Aggregate minimum size ${current_size} exceeds target ${target_bytes}; publishing latest/newest slice" >&2
		mv -f "$best_file" "$output_file"
		return 0
	fi

	while ((high <= max_probe)); do
		tmp_file=$(mktemp "$(dirname "$output_file")/.${output_file##*/}.try.XXXXXX") || {
			rm -f "$best_file"
			return 1
		}
		owner_build_json_array_from_db_try_limit "$owner_id_filter" "$repo_filter" "$tmp_file" "$high" || {
			status=$?
			rm -f "$tmp_file" "$best_file"
			return "$status"
		}
		current_size=$OWNER_ARRAY_LAST_SIZE

		if ((current_size <= target_bytes)); then
			mv -f "$tmp_file" "$best_file"
			low=$high
			((high *= 2))
			continue
		fi

		rm -f "$tmp_file"
		break
	done

	if ((high <= max_probe)); then
		while ((low + 1 < high)); do
			mid=$(((low + high) / 2))
			tmp_file=$(mktemp "$(dirname "$output_file")/.${output_file##*/}.try.XXXXXX") || {
				rm -f "$best_file"
				return 1
			}
			owner_build_json_array_from_db_try_limit "$owner_id_filter" "$repo_filter" "$tmp_file" "$mid" || {
				status=$?
				rm -f "$tmp_file" "$best_file"
				return "$status"
			}
			current_size=$OWNER_ARRAY_LAST_SIZE

			if ((current_size <= target_bytes)); then
				mv -f "$tmp_file" "$best_file"
				low=$mid
			else
				rm -f "$tmp_file"
				high=$mid
			fi
		done
	fi

	mv -f "$best_file" "$output_file"
}

owner_build_repo_json_arrays_from_db() {
	[ -n "$1" ] || return
	[ -n "$2" ] || return
	local owner_id_filter=$1
	local owner_name=$2
	local owner_repos=$3
	local owner_repo

	while IFS= read -r owner_repo; do
		[ -n "$owner_repo" ] || continue
		script_stop_requested && return 3
		mkdir -p "$BKG_INDEX_DIR/$owner_name/$owner_repo" || return $?
		owner_build_json_array_from_db_to_file "$owner_id_filter" "$owner_repo" "$BKG_INDEX_DIR/$owner_name/$owner_repo" "$BKG_INDEX_DIR/$owner_name/$owner_repo/.json.tmp" || return $?
	done <<<"$owner_repos"
}

owner_build_json_array() {
	[ -n "$1" ] || return
	local output_file
	local status=0

	check_script_timeout || return $?
	stop_requested && return 3
	output_file=$(mktemp) || return 1
	owner_build_json_array_to_file "$1" "$output_file" || status=$?
	if ((status == 0)); then
		cat "$output_file"
	fi
	rm -f "$output_file"
	return "$status"
}

owner_build_repo_json_arrays() {
	[ -n "$1" ] || return
	[ -n "$2" ] || return
	local owner_name=$1
	local owner_repos=$2
	local owner_repo

	while IFS= read -r owner_repo; do
		[ -n "$owner_repo" ] || continue
		script_stop_requested && return 3
		owner_build_json_array_to_file "$BKG_INDEX_DIR/$owner_name/$owner_repo" "$BKG_INDEX_DIR/$owner_name/$owner_repo/.json.tmp" || return $?
	done <<<"$owner_repos"

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
		local packages_table_sql
		local versions_table_sql
		local table_prefix_glob
		echo "$owner was opted out!"
		rm -rf "$BKG_INDEX_DIR/${owner:?}"
		packages_table_sql=$(sqlite_quote_identifier "$BKG_INDEX_TBL_PKG")
		versions_table_sql=$(sqlite_quote_identifier "$BKG_INDEX_TBL_VER")
		table_prefix_glob=$(sqlite_quote_literal "${BKG_INDEX_TBL_VER}_*")
		sqlite3 "$BKG_INDEX_DB" "delete from $packages_table_sql where owner_id=$(sqlite_quote_literal "$owner_id");"
		sqlite3 "$BKG_INDEX_DB" "delete from $versions_table_sql where owner_id=$(sqlite_quote_literal "$owner_id");"
		sqlite3 "$BKG_INDEX_DB" "select name from sqlite_master where type='table' and name glob $table_prefix_glob;" | while IFS= read -r table_name; do
			[ -n "$table_name" ] || continue
			[ "$(cut -d'_' -f4 <<<"$table_name")" = "$owner" ] || continue
			sqlite3 "$BKG_INDEX_DB" "drop table if exists $(sqlite_quote_identifier "$table_name");"
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
	local batch_first_started=""
	start_page=$(get_BKG BKG_PAGE_"$owner_id")
	batch_first_started=$(current_batch_first_started)
	[ -n "$batch_first_started" ] || batch_first_started="0000-00-00"

	if awk -F'|' -v owner_id_key="$owner_id" -v owner_key="$owner" '$1 == owner_id_key && $2 == owner_key { found = 1; exit } END { exit !found }' packages_already_updated && [ -z "$start_page" ]; then
		run_parallel save_package "$(sqlite3 "$BKG_INDEX_DB" "select package_type, package, max(date) as max_date from $(sqlite_quote_identifier "$BKG_INDEX_TBL_PKG") where owner_id = $(sqlite_quote_literal "$owner_id") group by package_type, package having max(date) < $(sqlite_quote_literal "$batch_first_started") order by max_date asc;" | awk -F'|' '{print "////"$1"//"$2}')"
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
	owner_has_packages=$(sqlite3 "$BKG_INDEX_DB" "select 1 from $(sqlite_quote_identifier "$BKG_INDEX_TBL_PKG") where owner_id=$(sqlite_quote_literal "$owner_id") limit 1;" 2>/dev/null || :)
	owner_repos=$(owner_repo_names_from_db "$owner_id")
	if [ -z "$owner_repos" ]; then
		owner_repos=$(find "$BKG_INDEX_DIR/$owner" -mindepth 1 -maxdepth 1 -type d -print0 | xargs -0 -I {} basename {})
	fi
	if [ -z "$owner_repos" ] && ! [[ "$owner_has_packages" =~ ^1$ ]]; then
		remember_scanned_owner_without_packages || return $?
	fi

	if [ -n "$owner_repos" ]; then
		local owner_json_tmp="$BKG_INDEX_DIR/$owner/.json.tmp"
		local owner_array_status=0

		echo "Creating $owner array..."
		check_script_timeout || return $?
		stop_requested && return 3
		if [[ "$owner_has_packages" =~ ^1$ ]]; then
			owner_build_json_array_from_db_to_file "$owner_id" "" "$BKG_INDEX_DIR/$owner" "$owner_json_tmp" || owner_array_status=$?
		else
			owner_build_json_array_to_file "$BKG_INDEX_DIR/$owner" "$owner_json_tmp" || owner_array_status=$?
		fi
		if ((owner_array_status != 0)); then
			rm -f "$owner_json_tmp"
			return "$owner_array_status"
		fi
		mv -f "$owner_json_tmp" "$BKG_INDEX_DIR/$owner/.json"
		run_command_with_stop_check bash "$(ytoxt_script_path)" "$BKG_INDEX_DIR/$owner/.json" || return $?

		echo "Creating $owner repo arrays..."
		if [[ "$owner_has_packages" =~ ^1$ ]]; then
			run_command_with_stop_check owner_build_repo_json_arrays_from_db "$owner_id" "$owner" "$owner_repos" || return $?
		else
			run_command_with_stop_check owner_build_repo_json_arrays "$owner" "$owner_repos" || return $?
		fi
		xargs -I {} mv -f "$BKG_INDEX_DIR/$owner/{}/.json.tmp" "$BKG_INDEX_DIR/$owner/{}/.json" 2>/dev/null <<<"$owner_repos"
		script_stop_requested && return 3
		while IFS= read -r owner_repo; do
			[ -n "$owner_repo" ] || continue
			run_command_with_stop_check bash "$(ytoxt_script_path)" "$BKG_INDEX_DIR/$owner/$owner_repo/.json" || return $?
		done <<<"$owner_repos"
	fi

	sed -i '/^\(.*\/\)*'"$owner"'$/d' "$BKG_OWNERS"
	echo "Updated $owner"
}
