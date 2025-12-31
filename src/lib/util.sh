#!/bin/bash
# Backage library
# Usage: ./lib.sh
# Dependencies: git curl jq parallel sqlite3 sqlite3-pcre zstd libxml2-utils, yq
# Copyright (c) ipitio
#
# shellcheck disable=SC1090,SC1091,SC2015,SC2034

set -o allexport

sudonot() {
    # shellcheck disable=SC2068
    if command -v sudo >/dev/null; then
        sudo -E "${@:-:}" || "${@:-:}"
    else
        "${@:-:}"
    fi
}

apt_install() {
    if ! dpkg -s "$@" &>/dev/null; then
        sudonot apt-get update
        sudonot apt-get install -yqq "$@"
    fi
}

yq_install() {
    [ ! -f /usr/bin/yq ] || sudonot mv -f /usr/bin/yq /usr/bin/yq.bak
    sudonot curl -LNZo /usr/bin/yq https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64
    sudonot chmod +x /usr/bin/yq
}

echo "Verifying dependencies..."
apt_install git curl jq parallel sqlite3 sqlite3-pcre zstd libxml2-utils
yq -V | grep -q mikefarah 2>/dev/null || yq_install
echo "Dependencies verified!"
# shellcheck disable=SC2046
source $(which env_parallel.bash)
env_parallel --session
GITHUB_OWNER=${GITHUB_OWNER:-ipitio}
GITHUB_REPO=${GITHUB_REPO:-backage}
BKG_ROOT=..
BKG_ENV=env.env
BKG_OWNERS=$BKG_ROOT/owners.txt
BKG_OPTOUT=$BKG_ROOT/optout.txt
BKG_INDEX_DB=$BKG_ROOT/index.db
BKG_INDEX_SQL=$BKG_ROOT/index.sql
BKG_INDEX_DIR=$BKG_ROOT/index
BKG_INDEX_TBL_OWN=owners
BKG_INDEX_TBL_PKG=packages
BKG_INDEX_TBL_VER=versions
BKG_MODE=0
BKG_MAX_LEN=16200
BKG_IS_FIRST=false
BKG_PAGE_ALL=1

# format numbers like 1000 to 1k
numfmt() {
    awk '{ split("k M B T P E Z Y", v); s=0; while( $1>999.9 ) { $1/=1000; s++ } print int($1*10)/10 v[s] }'
}

# format bytes to KB, MB, GB, etc.
numfmt_size() {
    # use sed to remove trailing \s*$
    awk '{ split("kB MB GB TB PB EB ZB YB", v); s=0; while( $1>999.9 ) { $1/=1000; s++ } print int($1*10)/10 " " v[s] }' | sed 's/[[:blank:]]*$//'
}

fmtsize_num() {
    awk '{
        if ($2) {
            u = substr($2, 1, 1)
            p = index("kMGTPEZY", u)
            if (p > 0) {
                m = match($2, /i?B$/) ? (match($2, /iB$/) ? 1024 : 1000) : (match($2, /b$/) ? 125 : 1024)
                $1 *= m ^ p
            }
        }
        print int($1)
    }'
}

sqlite3() {
    command sqlite3 -init <(echo "
.output /dev/null
.timeout 100000
.load /usr/lib/sqlite3/pcre.so
PRAGMA synchronous = OFF;
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = OFF;
PRAGMA locking_mode = NORMAL;
PRAGMA cache_size = -500000;
.output stdout
") "$@" 2>/dev/null
}

get_BKG() {
    [ -f "$BKG_ENV" ] || return
    while [ -f "$BKG_ENV.lock" ]; do :; done
    grep "^$1=" "$BKG_ENV" | cut -d'=' -f2
}

set_BKG() {
    local value
    local tmp_file
    value=$(echo "$2" | perl -pe 'chomp if eof')
    tmp_file=$(mktemp)
    [ -f "$BKG_ENV" ] || return
    until ln "$BKG_ENV" "$BKG_ENV.lock" 2>/dev/null; do :; done

    if ! grep -q "^$1=" "$BKG_ENV"; then
        echo "$1=$value" >>"$BKG_ENV"
    else
        grep -v "^$1=" "$BKG_ENV" >"$tmp_file"
        echo "$1=$value" >>"$tmp_file"
        mv "$tmp_file" "$BKG_ENV"
    fi

    sed -i '/^\s*$/d' "$BKG_ENV"
    echo >>"$BKG_ENV"
    rm -f "$BKG_ENV.lock"
}

get_BKG_set() {
    get_BKG "$1" | perl -pe 's/^\\n//' | perl -pe 's/\\n$//' | perl -pe 's/\\n\\n/\\n/' | perl -pe 's/\\n/\n/g'
}

set_BKG_set() {
    local list
    local code=0
    until ln "$BKG_ENV" "$BKG_ENV.$1.lock" 2>/dev/null; do :; done
    list=$(get_BKG_set "$1" | awk '!seen[$0]++' | perl -pe 's/\n/\\n/g')
    # shellcheck disable=SC2076
    [[ "$list" =~ "$2" ]] && code=1 || list="${list:+$list\n}$2"
    set_BKG "$1" "$(echo "$list" | perl -pe 's/\\n/\n/g' | perl -pe 's/\n/\\n/g' | perl -pe 's/^\\n//')"
    rm -f "$BKG_ENV.$1.lock"
    return $code
}

del_BKG() {
    [ -f "$BKG_ENV" ] || return
    until ln "$BKG_ENV" "$BKG_ENV.lock" 2>/dev/null; do :; done
    sed -i "/^$1=/d;/^\s*$/d" "$BKG_ENV"
    echo >>"$BKG_ENV"
    rm -f "$BKG_ENV.lock"
}

save_and_exit() {
    ((BKG_MAX_LEN > 0)) || return
    local to
    to=$(get_BKG BKG_TIMEOUT)

    if ((${to:-0} == 0)); then
        set_BKG BKG_TIMEOUT "1"
        echo "Stopping $$..."
    fi

    return 3
}

# shellcheck disable=SC2120
check_limit() {
    local total_calls
    local rate_limit_end
    local script_limit_diff
    local rate_limit_diff
    local hours_passed
    local remaining_time
    local minute_calls
    local sec_limit_diff
    local min_passed
    local rate_limit_start
    rate_limit_end=$(date -u +%s)
    [ -n "$BKG_SCRIPT_START" ] && rate_limit_start="$BKG_SCRIPT_START" || {
        rate_limit_start=$(get_BKG BKG_SCRIPT_START)
        [ -n "$rate_limit_start" ] || echo "BKG_SCRIPT_START empty!"
    }
    script_limit_diff=$((rate_limit_end - rate_limit_start))
    ((script_limit_diff < BKG_MAX_LEN)) || save_and_exit
    (($? != 3)) || return 3
    total_calls=$(get_BKG BKG_CALLS_TO_API)
    rate_limit_start=$(get_BKG BKG_RATE_LIMIT_START)
    [ -n "$rate_limit_start" ] || set_BKG BKG_RATE_LIMIT_START "$rate_limit_end"
    rate_limit_diff=$((rate_limit_end - rate_limit_start))
    hours_passed=$((rate_limit_diff / 3600))

    if ((total_calls >= 1000 * (hours_passed + 1))); then
        echo "$total_calls calls to the GitHub API in $((rate_limit_diff / 60)) minutes"
        remaining_time=$((3600 * (hours_passed + 1) - rate_limit_diff))
        ((remaining_time < BKG_MAX_LEN - script_limit_diff)) || save_and_exit
        (($? != 3)) || return 3
        start=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
        end=$(date -u -d "+${remaining_time} seconds" +'%Y-%m-%dT%H:%M:%SZ')
        echo "Sleeping for $remaining_time seconds from $start to $end..."
        sleep $remaining_time
        echo "Resuming!"
        set_BKG BKG_RATE_LIMIT_START "$(date -u +%s)"
        set_BKG BKG_CALLS_TO_API "0"
    fi

    # wait if 900 or more calls have been made in the last minute
    minute_calls=$(get_BKG BKG_MIN_CALLS_TO_API)
    rate_limit_start=$(get_BKG BKG_MIN_RATE_LIMIT_START)
    [ -n "$rate_limit_start" ] || echo "BKG_MIN_RATE_LIMIT_START empty!"
    sec_limit_diff=$(($(date -u +%s) - rate_limit_start))
    min_passed=$((sec_limit_diff / 60))

    if ((minute_calls >= 900 * (min_passed + 1))); then
        echo "$minute_calls calls to the GitHub API in $sec_limit_diff seconds"
        remaining_time=$((60 * (min_passed + 1) - sec_limit_diff))
        ((remaining_time < BKG_MAX_LEN - script_limit_diff)) || save_and_exit
        (($? != 3)) || return 3
        start=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
        end=$(date -u -d "+${remaining_time} seconds" +'%Y-%m-%dT%H:%M:%SZ')
        echo "Sleeping for $remaining_time seconds from $start to $end..."
        sleep $remaining_time
        echo "Resuming!"
        set_BKG BKG_MIN_RATE_LIMIT_START "$(date -u +%s)"
        set_BKG BKG_MIN_CALLS_TO_API "0"
    fi
}

curl() {
    # if connection times out or max time is reached, wait increasing amounts of time before retrying
    local i=2
    local max_attempts=7
    local wait_time=1
    local result

    while [ "$i" -lt "$max_attempts" ]; do
        result=$(command curl -sSLNZ --connect-timeout 60 -m 120 --retry 5 --retry-delay 1 --retry-all-errors "$@" 2>/dev/null)
        [ -n "$result" ] && echo "$result" && return 0
        check_limit || return $?
        sleep "$wait_time"
        ((i++))
        ((wait_time *= i))
    done

    echo ""
    return 1
}

run_parallel() {
    local code
    local exit_code
    exit_code=$(mktemp)

    if [ "$(wc -l <<<"$2")" -gt 1 ]; then
        ( # parallel --lb --halt soon,fail=1 -j "$max_jobs"
            local active=0
            local max_jobs
            max_jobs=$(nproc --all)

            for i in $2; do
                code=$(cat "$exit_code")
                ! grep -q "3" <<<"$code" || exit
                ! grep -q "2" <<<"$code" || break

                while [ "$active" -ge "$max_jobs" ]; do
                    wait -n
                    ((active--))
                done

                ("$1" "$i" || echo "$?" >>"$exit_code") &
                ((active++))
            done

            wait
        ) &

        wait "$!"
    else
        "$1" "$2" || echo "$?" >>"$exit_code"
    fi

    code=$(cat "$exit_code")
    rm -f "$exit_code"
    ! grep -q "3" <<<"$code" || return 3
}

_jq() {
    echo "$1" | base64 --decode | jq -r "${@:2}"
}

dldb() {
    local latest=${1:-$(curl "https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}/releases/latest" | grep -oP "href=\"/${GITHUB_OWNER}/${GITHUB_REPO}/releases/tag/[^\"]+" | cut -d'/' -f6)}
    [[ "$(curl -o /dev/null --silent -Iw '%{http_code}' "https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}/releases/download/$latest/index.sql.zst")" != "404" ]] || return 1
    [ -z "$2" ] || return 0
    echo "Downloading the latest database..."
    # `cd src ; source bkg.sh && dldb` to dl the latest db
    [ ! -f "$BKG_INDEX_DB" ] || mv "$BKG_INDEX_DB" "$BKG_INDEX_DB".bak
    command curl -sSLNZ "https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}/releases/download/$latest/index.sql.zst" | unzstd -v -c | sqlite3 "$BKG_INDEX_DB"

    if [ -f "$BKG_INDEX_DB" ]; then
        [ ! -f "$BKG_INDEX_DB".bak ] || rm -f "$BKG_INDEX_DB".bak
    else
        [ ! -f "$BKG_INDEX_DB".bak ] || mv "$BKG_INDEX_DB".bak "$BKG_INDEX_DB"
        echo "Failed to get the latest database"
    fi

    [ -f "$BKG_ROOT/.gitignore" ] || echo "index.db*" >>$BKG_ROOT/.gitignore
    grep -q "index.db" "$BKG_ROOT/.gitignore" || echo "index.db*" >>$BKG_ROOT/.gitignore
}

curl_gh() {
    curl -H "Accept: application/vnd.github+json" -H "Authorization: Bearer $GITHUB_TOKEN" -H "X-GitHub-Api-Version: 2022-11-28" "$@"
}

query_api() {
    local res
    local calls_to_api
    local min_calls_to_api

    res=$(curl_gh "https://api.github.com/$1")
    (($? != 3)) || return 3
    calls_to_api=$(get_BKG BKG_CALLS_TO_API)
    min_calls_to_api=$(get_BKG BKG_MIN_CALLS_TO_API)
    ((calls_to_api++))
    ((min_calls_to_api++))
    set_BKG BKG_CALLS_TO_API "$calls_to_api"
    set_BKG BKG_MIN_CALLS_TO_API "$min_calls_to_api"
    echo "$res"
}

check_db() {
    local release
    local latest
    release=$(query_api "repos/${GITHUB_OWNER}/${GITHUB_REPO}/releases/latest")
    latest=$(jq -r '.tag_name' <<<"$release")

    until dldb "$latest" 1; do
        echo "Deleting the latest release..."
        curl_gh -X DELETE "https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/releases/$(jq -r '.id' <<<"$release")"
        release=$(query_api "repos/${GITHUB_OWNER}/${GITHUB_REPO}/releases/latest")
        latest=$(jq -r '.tag_name' <<<"$release")
    done
}

docker_manifest_size() {
    local manifest=$1

    if [[ -n "$(jq '.. | try .layers[]' 2>/dev/null <<<"$manifest")" ]]; then
        jq '.. | try .size | select(. > 0)' <<<"$manifest" | awk '{s+=$1} END {printf "%d",  s}'
    elif [[ -n "$(jq '.. | try .manifests[]' 2>/dev/null <<<"$manifest")" ]]; then
        jq '.. | try .size | select(. > 0)' <<<"$manifest" | awk '{s+=$1} END {printf "%d",  s/NR}'
    else
        echo -1
    fi
}

owner_get_id() {
    local owner
    local owner_id=""
    owner=$(echo "$1" | tr -d '[:space:]')
    [ -n "$owner" ] || return

    if [[ "$owner" =~ .*\/.* ]]; then
        owner_id=$(cut -d'/' -f1 <<<"$owner")
        owner=$(cut -d'/' -f2 <<<"$owner")
    fi

    if [[ ! "$owner_id" =~ ^[1-9] ]]; then
        owner_id=$(curl "https://github.com/$owner" | grep -zoP 'meta.*?u\/\d+' | tr -d '\0' | grep -oP 'u\/\d+' | sort -u | head -n1 | grep -oP '\d+')

        if [[ ! "$owner_id" =~ ^[1-9] && -n "$GITHUB_TOKEN" ]]; then
            owner_id=$(query_api "users/$owner")
            (($? != 3)) || return 3
            owner_id=$(jq -r '.id' <<<"$owner_id")

            if [[ ! "$owner_id" =~ ^[1-9] ]]; then
                owner_id=$(query_api "orgs/$owner")
                (($? != 3)) || return 3
                owner_id=$(jq -r '.id' <<<"$owner_id") || return 1
            fi
        fi
    fi

    echo "$owner_id/$owner"
}

owner_has_packages() {
    local owner=$1
    local owner_type
    [ -n "$(curl "https://github.com/orgs/$owner/people" | grep -zoP 'href="/orgs/'"$owner"'/people"' | tr -d '\0')" ] && owner_type="orgs" || owner_type="users"
    packages_lines=$(grep -zoP 'href="/'"$owner_type"'/'"$owner"'/packages/[^/]+/package/[^"]+"' <([ "$owner_type" = "users" ] && curl "https://github.com/$owner?tab=packages$([ "$BKG_MODE" -lt 2 ] && echo "&visibility=public" || { [ "$BKG_MODE" -eq 5 ] && echo "&visibility=private" || echo ""; })&&per_page=1&page=1" || curl "https://github.com/$owner_type/$owner/packages?per_page=1$([ "$BKG_MODE" -lt 2 ] && echo "&visibility=public" || { [ "$BKG_MODE" -eq 5 ] && echo "&visibility=private" || echo ""; })&page=1") | tr -d '\0')
    [ -n "$packages_lines" ] || return 1
}

get_owners() {
    sort -u <<<"$1" | while read -r owner; do owner_get_id "$owner"; done | grep -v '^\/'
}

curl_users() {
    local users
    users="$(curl "https://github.com/$1" | grep -oP 'href="/.+?".*>' | tr -d '\0' | grep -Ev '( .*|\?(return_to|tab))=' | tr -d '\0' | grep -oP '/.*?"' | cut -c2- | rev | cut -c2- | rev | grep -v "/")"
    [ -z "$2" ] && echo "$users" || get_owners "$users"
}

curl_orgs() {
    local orgs
    orgs="$(curl "https://github.com/$1" | grep -oP '/orgs/[^/]+' | tr -d '\0' | cut -d'/' -f3)"
    [ -z "$2" ] && echo "$orgs" || get_owners "$orgs"
}

explore() {
    local node=$1
	local is_user=false
	local got_orgs=false
    [[ "$node" =~ .*\/.* ]] && local graph=("stargazers" "watchers" "forks") || local graph=("followers" "following" "people")
    [ -z "$2" ] || graph=("$2")

    for edge in "${graph[@]}"; do
        local page=1
        while true; do
            local nodes

            if [[ "$node" =~ .*\/.* ]]; then
                nodes=$(curl_users "$node/$edge?page=$page") # repo
            else
				if [ "$is_user" = false ]; then
                	nodes=$(curl_users "orgs/$node/$edge?page=$page") # org
					[ -n "$nodes" ] || is_user=true
				fi

				if [ "$is_user" = true ]; then
					nodes=$(curl_users "$node?tab=$edge&page=$page") # user

					if [ "$got_orgs" = false ]; then
						curl_orgs "$node"
						got_orgs=true
					fi
				fi
            fi

            grep -v "$(cut -d'/' -f1 <<<"$node")" <<<"$nodes"
            [[ "$(wc -l <<<"$nodes")" -ge 15 ]] || break
            ((page++))
        done
    done
}

get_membership() {
    local owner
    owner=$(cut -d'/' -f2 <<<"$1")

    if [ -n "$(grep -zoP 'href="/orgs/'"$owner"'/people"' <<<"$(curl "https://github.com/orgs/$owner/people")" | tr -d '\0')" ]; then
        explore "$owner" "people"
    else
        curl_orgs "$owner"
    fi
}

ytox() {
    echo "<?xml version=\"1.0\" encoding=\"UTF-8\"?><xml>$(yq -ox -I0 "$1" | sed 's/"/\\"/g')</xml>" >"${1%.*}.xml" 2>/dev/null
    stat -c %s "${1%.*}.xml" || echo -1
}

ytoxt() {
    # ytox + trim: if the json or xml is over 50MB, remove oldest versions
    local f="$1"
    local tmp
    local del_n=1
    local last_xml_size=-1

    [ -f "$f" ] || return 1

    tmp=$(mktemp "${f}.XXXXXX") || return 1
    trap 'rm -f "$tmp"' RETURN

    while [ -f "$f" ]; do
        local json_size
        local xml_size
        local tmp_size

        json_size=$(stat -c %s "$f" 2>/dev/null || echo -1)

        if [ "$json_size" -lt 50000000 ]; then
            # Only generate/check XML if JSON is already under limit.
            xml_size=$(ytox "$f" 2>/dev/null || echo -1)
            # If XML size can't be determined, treat it as oversized so we keep trimming.
            [ "$xml_size" -ge 0 ] || xml_size=50000000

            if [ "$xml_size" -lt 50000000 ]; then
                break
            fi

            # If XML is still too large, keep trimming, but avoid redoing work forever.
            if [ "$xml_size" -eq "$last_xml_size" ] && [ "$last_xml_size" -ge 0 ]; then
                break
            fi
            last_xml_size="$xml_size"

            # XML still too large: increase trimming aggressiveness as well.
            if [ "$del_n" -lt 65536 ]; then
                del_n=$((del_n * 2))
            fi
        else
            # JSON is still too large: increase trimming aggressiveness.
            if [ "$json_size" -ge 50000000 ]; then
                if [ "$del_n" -lt 65536 ]; then
                    del_n=$((del_n * 2))
                fi
            fi
        fi

        if jq -e '
            if (type == "array") or (type == "object") then
                any(.[]; ((.version // []) | type == "array") and ((.version // []) | length > 0))
            else
                ((.version // []) | type == "array") and ((.version // []) | length > 0)
            end
        ' "$f" >/dev/null; then
            jq -c '
                def id_to_num:
                    if type == "number" then .
                    elif type == "string" then tonumber? // 0
                    else 0 end;
                def vlen:
                    (.version // []) | if type == "array" then length else 0 end;
                def trim_versions($n):
                    if ((.version // []) | type == "array") and ((.version // []) | length > 0) then
                        (
                            .version
                            | sort_by(.id | id_to_num)
                            | .[$n:]
                        ) as $v
                        | .version = $v
                    else
                        .
                    end;
                if type == "array" then
                    (to_entries
                    | (max_by(.value | vlen) // empty) as $max
                    | map(
                        if .key == $max.key and ((.value | vlen) > 0)
                        then (.value |= trim_versions($n))
                        else .
                        end
                    )
                    | map(.value))
                elif type == "object" then
                    (to_entries
                    | (max_by(.value | vlen) // empty) as $max
                    | map(
                        if .key == $max.key and ((.value | vlen) > 0)
                        then (.value |= trim_versions($n))
                        else .
                        end
                    )
                    | from_entries)
                else
                    trim_versions($n)
                end
            ' --argjson n "$del_n" "$f" >"$tmp"
        else
            jq -c '
                if type == "array" then
                    (
                        def to_num:
                            if type == "number" then .
                            elif type == "string" then tonumber? // 0
                            else 0 end;
                        to_entries
                        | (min_by([ (.value.raw_downloads // 0 | to_num), (.value.date // "") ]) // null) as $target
                        | if $target == null then
                            map(.value)
                        else
                            [ .[] | select(.key != $target.key) | .value ]
                        end
                    )
                elif type == "object" then
                    (
                        def to_num:
                            if type == "number" then .
                            elif type == "string" then tonumber? // 0
                            else 0 end;
                        to_entries
                        | (min_by([ (.value.raw_downloads // 0 | to_num), (.value.date // "") ]) // null) as $target
                        | if $target == null then
                            from_entries
                        else
                            ([ .[] | select(.key != $target.key) ] | from_entries)
                        end
                    )
                else
                    .
                end
                ' "$f" >"$tmp"
        fi

        tmp_size=$(stat -c %s "$tmp" 2>/dev/null || echo -1)

        # If trimming didn't reduce size, retry with more aggressive deletion instead of stalling.
        if [ "$json_size" -ge 0 ] && [ "$tmp_size" -ge 0 ] && [ "$tmp_size" -ge "$json_size" ]; then
            rm -f "$tmp"

            if [ "$del_n" -lt 65536 ]; then
                del_n=$((del_n * 2))
                continue
            fi

            # If we're already at max aggressiveness, fall back to trimming whole packages once.
            jq -c '
                if type == "array" then
                    (
                        def to_num:
                            if type == "number" then .
                            elif type == "string" then tonumber? // 0
                            else 0 end;
                        to_entries
                        | (min_by([ (.value.raw_downloads // 0 | to_num), (.value.date // "") ]) // null) as $target
                        | if $target == null then
                            map(.value)
                        else
                            [ .[] | select(.key != $target.key) | .value ]
                        end
                    )
                elif type == "object" then
                    (
                        def to_num:
                            if type == "number" then .
                            elif type == "string" then tonumber? // 0
                            else 0 end;
                        to_entries
                        | (min_by([ (.value.raw_downloads // 0 | to_num), (.value.date // "") ]) // null) as $target
                        | if $target == null then
                            from_entries
                        else
                            ([ .[] | select(.key != $target.key) ] | from_entries)
                        end
                    )
                else
                    .
                end
            ' "$f" >"$tmp"

            tmp_size=$(stat -c %s "$tmp" 2>/dev/null || echo -1)
            if [ "$json_size" -ge 0 ] && [ "$tmp_size" -ge 0 ] && [ "$tmp_size" -ge "$json_size" ]; then
                rm -f "$tmp"
                break
            fi
        fi

        mv "$tmp" "$f"
    done

    # Ensure the XML output corresponds to the final JSON.
    ytox "$f" >/dev/null 2>&1

	# If either JSON or XML is > 100MB, empty each one that is too large:
	[ "$(stat -c %s "$f" 2>/dev/null || echo -1)" -lt 100000000 ] || echo "{}" >"$f"
	[ "$(stat -c %s "${f%.*}.xml" 2>/dev/null || echo -1)" -lt 100000000 ] || echo "<?xml version=\"1.0\" encoding=\"UTF-8\"?><xml></xml>" >"${f%.*}.xml"
}

ytoy() {
    yq -oy "$1" | sed 's/"/\\"/g' >"${1%.*}.yml"
}

clean_owners() {
    local temp_file
    temp_file=$(mktemp)
    echo >>"$1"
    awk 'NF' "$1" >"$temp_file" && cp -f "$temp_file" "$1"
    sed -i 's/"//g; s/^[[:space:]]*//;s/[[:space:]]*$//; /^$/d; /^0\/$/d; /^null\/.*/d; /^\(.*\/\)*\(solutions\|sponsors\|enterprise\|premium-support\)$/d' "$1"
    awk '!seen[$0]++' "$1" >"$temp_file" && cp -f "$temp_file" "$1"
}

set +o allexport
