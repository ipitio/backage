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
		run_parallel update_package "$refresh_refs"
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

owner_array_db_fallback_version_limit() {
	local version_limit=${BKG_OWNER_ARRAY_DB_FALLBACK_VERSION_LIMIT:-2}

	[[ "$version_limit" =~ ^-?[0-9]+$ ]] || version_limit=2
	printf '%s\n' "$version_limit"
}

owner_array_db_estimated_version_limit() {
	[ -n "$1" ] || return
	local owner_id_filter=$1
	local repo_filter=${2:-}
	local target_bytes=${3:-$(owner_array_target_bytes)}
	local owner_id_sql
	local repo_filter_sql=""
	local packages_table_sql
	local versions_table_sql
	local legacy_prefix_sql
	local effective_target
	local headroom_percent=${BKG_OWNER_ARRAY_DB_ESTIMATE_HEADROOM_PERCENT:-75}
	local fallback_limit
	local estimated_limit

	[[ "$target_bytes" =~ ^[1-9][0-9]*$ ]] || target_bytes=$(owner_array_target_bytes)
	[[ "$headroom_percent" =~ ^[1-9][0-9]*$ ]] || headroom_percent=75
	((headroom_percent <= 100)) || headroom_percent=100
	effective_target=$((target_bytes * headroom_percent / 100))
	((effective_target > 0)) || effective_target=$target_bytes

	fallback_limit=$(owner_array_db_fallback_version_limit)
	owner_id_sql=$(sqlite_quote_literal "$owner_id_filter")
	packages_table_sql=$(sqlite_quote_identifier "$BKG_INDEX_TBL_PKG")
	versions_table_sql=$(sqlite_quote_identifier "$BKG_INDEX_TBL_VER")
	legacy_prefix_sql=$(sqlite_quote_literal "${BKG_INDEX_TBL_VER}_")
	if [ -n "$repo_filter" ]; then
		repo_filter_sql="and p.repo = $(sqlite_quote_literal "$repo_filter")"
	fi

	estimated_limit=$(sqlite3 "$BKG_INDEX_DB" "
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
				p.date
			from $packages_table_sql p
			join latest_dates l
			  on p.owner_id = l.owner_id
			 and p.package = l.package
			 and p.date = l.latest_date
			where p.owner_id = $owner_id_sql
			  $repo_filter_sql
		),
		version_candidates as (
			select
				v.owner_id,
				v.owner_type,
				v.package_type,
				v.owner,
				v.repo,
				v.package,
				v.id,
				v.name,
				v.size,
				v.downloads,
				v.downloads_month,
				v.downloads_week,
				v.downloads_day,
				v.date,
				v.tags,
				case when v.id != '' and v.id not glob '*[^0-9]*' then cast(v.id as integer) end as numeric_id,
				replace(replace(replace(replace(coalesce(v.tags, ''), ' ', ''), char(9), ''), char(10), ''), char(13), '') as compact_tags,
				row_number() over (
					partition by v.owner_id, v.package_type, v.repo, v.package, v.id
					order by v.date desc
				) as version_date_rank
			from $versions_table_sql v
			join latest_packages p
			  on v.owner_id = p.owner_id
			 and v.owner_type = p.owner_type
			 and v.package_type = p.package_type
			 and v.owner = p.owner
			 and v.repo = p.repo
			 and v.package = p.package
			 and v.date >= p.date
		),
		version_rows as (
			select *
			from version_candidates
			where version_date_rank = 1
		),
		legacy_fallback_packages as (
			select 1
			from latest_packages p
			where not exists (
				select 1
				from $versions_table_sql v
				where v.owner_id = p.owner_id
				  and v.owner_type = p.owner_type
				  and v.package_type = p.package_type
				  and v.owner = p.owner
				  and v.repo = p.repo
				  and v.package = p.package
				  and v.date >= p.date
				limit 1
			)
			  and exists (
				select 1
				from sqlite_master sm
				where sm.type = 'table'
				  and sm.name = ($legacy_prefix_sql || p.owner_type || '_' || p.package_type || '_' || p.owner || '_' || p.repo || '_' || p.package)
				limit 1
			)
		),
		package_marks as (
			select
				owner_id,
				owner_type,
				package_type,
				owner,
				repo,
				package,
				max(numeric_id) as newest_numeric_id,
				coalesce(
					max(case when numeric_id is not null and tags is not null and tags != '' and (',' || compact_tags || ',') like '%,latest,%' then numeric_id end),
					max(case when numeric_id is not null and tags is not null and tags != '' and instr(tags, '^') = 0 and instr(tags, '~') = 0 and instr(tags, '-') = 0 then numeric_id end),
					max(case when numeric_id is not null and tags is not null and tags != '' and instr(tags, '^') = 0 and instr(tags, '~') = 0 then numeric_id end),
					max(case when numeric_id is not null and tags is not null and tags != '' and instr(tags, '^') = 0 then numeric_id end),
					max(case when numeric_id is not null and tags is not null and tags != '' then numeric_id end)
				) as latest_numeric_id
			from version_rows
			group by owner_id, owner_type, package_type, owner, repo, package
		),
		ranked_versions as (
			select
				v.*,
				(
					240
					+ length(coalesce(v.id, ''))
					+ length(coalesce(v.name, ''))
					+ length(coalesce(v.date, ''))
					+ length(coalesce(v.tags, ''))
					+ length(cast(coalesce(v.size, -1) as text))
					+ length(cast(coalesce(v.downloads, -1) as text))
					+ length(cast(coalesce(v.downloads_month, -1) as text))
					+ length(cast(coalesce(v.downloads_week, -1) as text))
					+ length(cast(coalesce(v.downloads_day, -1) as text))
				) as estimated_version_bytes,
				row_number() over (
					partition by v.owner_id, v.package_type, v.repo, v.package
					order by
						case when v.numeric_id is null then 1 else 0 end desc,
						coalesce(v.numeric_id, 0) desc,
						v.id desc
				) as tail_rank,
				case
					when v.numeric_id is not null
					 and (
						v.numeric_id = m.newest_numeric_id
						or v.numeric_id = m.latest_numeric_id
					 )
					then 1
					else 0
				end as mandatory
			from version_rows v
			join package_marks m
			  on v.owner_id = m.owner_id
			 and v.owner_type = m.owner_type
			 and v.package_type = m.package_type
			 and v.owner = m.owner
			 and v.repo = m.repo
			 and v.package = m.package
		),
		base_estimate as (
			select
				coalesce((select count(*) from latest_packages), 0) * 900 + 2 as package_bytes,
				coalesce((select sum(estimated_version_bytes) from ranked_versions where mandatory = 1), 0) as mandatory_version_bytes
		),
		optional_rank_costs as (
			select tail_rank, sum(estimated_version_bytes) as rank_bytes
			from ranked_versions
			where mandatory = 0
			group by tail_rank
		),
		candidates as (
			select
				tail_rank as version_limit,
				(select package_bytes + mandatory_version_bytes from base_estimate)
					+ sum(rank_bytes) over (order by tail_rank rows between unbounded preceding and current row) as estimated_bytes
			from optional_rank_costs
		)
		select
			case
				when (select count(*) from latest_packages) = 0 then 0
				when (select package_bytes + mandatory_version_bytes from base_estimate) >= $effective_target then 0
				when (select count(*) from legacy_fallback_packages) > 0 then $fallback_limit
				else coalesce((select max(version_limit) from candidates where estimated_bytes <= $effective_target), 0)
			end;
	" 2>/dev/null || :)

	[[ "$estimated_limit" =~ ^[0-9]+$ ]] || estimated_limit=$fallback_limit
	printf '%s\n' "$estimated_limit"
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

legacy_owner_build_json_array_to_file() {
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

legacy_owner_repo_names_from_db() {
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
	# Fields and table_version_name are consumed through Bash dynamic scope.
	# shellcheck disable=SC2034
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

legacy_owner_build_json_array_from_db_to_file() {
	[ -n "$1" ] || return
	[ -n "$4" ] || return
	local owner_id_filter=$1
	local repo_filter=${2:-}
	local size_hint_dir=$3
	local output_file=$4
	local target_bytes
	local source_size=0
	local source_count=0
	local version_limit

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

	if [ -n "${BKG_OWNER_ARRAY_DB_VERSION_LIMIT+x}" ]; then
		version_limit=$BKG_OWNER_ARRAY_DB_VERSION_LIMIT
		[[ "$version_limit" =~ ^-?[0-9]+$ ]] || version_limit=$(owner_array_db_fallback_version_limit)
	else
		version_limit=$(owner_array_db_estimated_version_limit "$owner_id_filter" "$repo_filter" "$target_bytes")
	fi
	owner_build_json_array_from_db_limit_to_file "$owner_id_filter" "$repo_filter" "$output_file" "$version_limit"
}

legacy_owner_build_repo_json_arrays_from_db() {
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

legacy_owner_build_json_array() {
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

legacy_owner_build_repo_json_arrays() {
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

owner_repo_names_from_db() {
    [ -n "$1" ] || return
    bkg_python render repositories "$1"
}

owner_build_json_array_to_file() {
    [ -n "$1" ] || return
    [ -n "$2" ] || return
    bkg_python render aggregate-files "$1" "$2"
}

owner_build_json_array_from_db_to_file() {
    [ -n "$1" ] || return
    [ -n "$4" ] || return
    local repo_filter=${2:--}
    local size_hint_dir=${3:--}

    [ -n "$repo_filter" ] || repo_filter="-"
    [ -n "$size_hint_dir" ] || size_hint_dir="-"
    bkg_python render aggregate-database \
        "$1" "$repo_filter" "$size_hint_dir" "$4"
}

owner_build_repo_json_arrays_from_db() {
    [ -n "$1" ] || return
    [ -n "$2" ] || return
    local owner_repo

    while IFS= read -r owner_repo; do
        [ -n "$owner_repo" ] || continue
        script_stop_requested && return 3
        mkdir -p "$BKG_INDEX_DIR/$2/$owner_repo" || return $?
        owner_build_json_array_from_db_to_file \
            "$1" "$owner_repo" "$BKG_INDEX_DIR/$2/$owner_repo" \
            "$BKG_INDEX_DIR/$2/$owner_repo/.json.tmp" || return $?
    done <<<"$3"
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
    local owner_repo

    while IFS= read -r owner_repo; do
        [ -n "$owner_repo" ] || continue
        script_stop_requested && return 3
        owner_build_json_array_to_file \
            "$BKG_INDEX_DIR/$1/$owner_repo" \
            "$BKG_INDEX_DIR/$1/$owner_repo/.json.tmp" || return $?
    done <<<"$2"
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
	local owner_scan_reconciled=false
	local owner_scan_required=true
	local owner_partially_updated=false
	local pending_count=0
	local refresh_plan=""
	local refresh_refs=""
	local scan_completed=false
	local scan_status=0
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
		run_parallel update_package "$refresh_refs"
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

		for page in $(seq "$start_page" 100000); do
			local pages_left=0
			((page - start_page < 51)) || break
			page_package "$page"
			pages_left=$?
			if ((pages_left == 3)); then
				return 3
			elif ((pages_left != 0 && pages_left != 2)); then
				owner_scan_fail "owner package listing page $page failed" || return $?
				return 0
			fi
			if $PACKAGE_PAGE_OWNER_MISSING; then
				retire_missing_owner "$owner_id/$owner" || return $?
				owner_scan_clear_legacy_state
				return 0
			fi
			run_parallel update_package "$PACKAGE_PAGE_WORK"
			(($? != 3)) || return 3
			bkg_python package finish-page \
				"$owner_id" "$OWNER_SCAN_MARKER" "$page" \
				"$(date -u +%s)" || return $?

			if ((pages_left == 2)); then
				scan_completed=true
				break
			fi
		done

		if ! $scan_completed; then
			echo "Paused $owner owner scan at page $page"
			return 0
		fi

		owner_scan_verify_missing_packages
		scan_status=$?
		if ((scan_status == 3)); then
			return 3
		elif ((scan_status != 0)); then
			owner_scan_fail "known package verification failed" || return $?
			return 0
		fi
		owner_scan_complete || return $?
		owner_scan_reconciled=true
	fi

	local owner_repos
	local owner_has_packages
	cleanup_generated_json_sidecars "$BKG_INDEX_DIR/$owner"
	owner_has_packages=$(sqlite3 "$BKG_INDEX_DB" "select 1 from $(sqlite_quote_identifier "$BKG_INDEX_TBL_PKG") where owner_id=$(sqlite_quote_literal "$owner_id") limit 1;" 2>/dev/null || :)
	owner_repos=$(owner_repo_names_from_db "$owner_id")
	if [ -z "$owner_repos" ] && ! $owner_scan_reconciled; then
		owner_repos=$(find "$BKG_INDEX_DIR/$owner" -mindepth 1 -maxdepth 1 -type d -print0 | xargs -0 -I {} basename {})
	fi
	if [ -z "$owner_repos" ] && ! [[ "$owner_has_packages" =~ ^1$ ]]; then
		remember_scanned_owner_without_packages || return $?
		rm -f -- "$BKG_INDEX_DIR/$owner/.json" "$BKG_INDEX_DIR/$owner/.xml"
		rmdir "$BKG_INDEX_DIR/$owner" 2>/dev/null || :
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
