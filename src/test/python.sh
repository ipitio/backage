#!/bin/bash

set -euo pipefail

test_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
src_dir=$(cd "$test_dir/.." && pwd)
repo_dir=$(cd "$src_dir/.." && pwd)
workdir=$(mktemp -d)

cleanup() {
	rm -rf "$workdir"
}

trap cleanup EXIT

if ! command -v python3 >/dev/null 2>&1; then
	echo "Python migration tests require python3" >&2
	exit 1
fi

(
	cd "$repo_dir"
	uv sync --locked --quiet --no-install-project
	PYTHONPATH="$src_dir" PYTHONDONTWRITEBYTECODE=1 \
		uv run --locked --no-sync pytest -q "$test_dir"
)
python_bin="$repo_dir/.venv/bin/python"
export PATH="$repo_dir/.venv/bin:$PATH"

config_json=$(
	cd "$src_dir"
	env -i PATH="$PATH" PYTHONPATH="$src_dir" PYTHONDONTWRITEBYTECODE=1 \
		"$python_bin" -m bkg_py config
)

jq -e '
	.github_owner == "ipitio"
	and .github_repo == "backage"
	and (.env_file | endswith("/src/env.env"))
' <<<"$config_json" >/dev/null

branch_config_json=$(
	cd "$src_dir"
	env -i PATH="$PATH" PYTHONPATH="$src_dir" PYTHONDONTWRITEBYTECODE=1 \
		GITHUB_BRANCH=feature "$python_bin" -m bkg_py config
)

jq -e '
	.index_name == "index-feature"
	and (.index_db | endswith("/index-feature.db"))
	and (.index_sql | endswith("/index-feature.sql"))
	and (.index_dir | endswith("/index-feature"))
' <<<"$branch_config_json" >/dev/null

queue_dir="$workdir/owner-queue"
queue_repo="$queue_dir/index"
mkdir -p "$queue_repo"
printf '%s\n' alpha beta >"$queue_dir/connections"
printf '%s\n' manual >"$queue_dir/manual"
printf '%s\n' beta >"$queue_dir/all_owners_in_db"
: >"$queue_dir/owners_partially_updated"
: >"$queue_dir/owners_stale"
git -C "$queue_repo" init -q
git -C "$queue_repo" config user.name test
git -C "$queue_repo" config user.email test@example.com
printf '%s\n' README >"$queue_repo/README.md"
git -C "$queue_repo" add README.md
git -C "$queue_repo" commit -qm init

owner_candidates=$(
	cd "$queue_dir"
	PYTHONPATH="$src_dir" PYTHONDONTWRITEBYTECODE=1 \
		"$python_bin" -m bkg_py select-owners \
			0 "$queue_dir/connections" 10 current "$queue_dir/manual" "$queue_repo"
)
[ "$owner_candidates" = $'manual\ncurrent\nalpha' ] || {
	echo "Unexpected Python owner candidate selection:" >&2
	printf '%s\n' "$owner_candidates" >&2
	exit 1
}

wrapper_candidates=$(
	cd "$queue_dir"
	bash "$src_dir/lib/get.sh" \
		0 "$queue_dir/connections" 10 current "$queue_dir/manual" "$queue_repo"
)
[ "$wrapper_candidates" = "$owner_candidates" ] || {
	echo "lib/get.sh returned different owner candidates than Python" >&2
	exit 1
}

printf '%s\n' manual beta >"$queue_dir/all_owners_in_db"
manual_bypass_candidates=$(
	cd "$queue_dir"
	bash "$src_dir/lib/get.sh" \
		0 "$queue_dir/connections" 10 current "$queue_dir/manual" "$queue_repo"
)
grep -Fxq manual <<<"$manual_bypass_candidates" || {
	echo "Manual owners must bypass the discovered-owner database filter" >&2
	exit 1
}
! grep -Fxq beta <<<"$manual_bypass_candidates" || {
	echo "Known discovered owners must remain filtered" >&2
	exit 1
}

printf '%s\n' alpha beta >"$queue_dir/connections"
: >"$queue_dir/manual"
: >"$queue_dir/all_owners_in_db"
: >"$queue_dir/owners_partially_updated"
: >"$queue_dir/owners_stale"
printf '%s\t%s\n' alpha 9999999999 >"$queue_dir/owners_deferred"
reasons_file="$queue_dir/reasons"
deferred_candidates=$(
	cd "$queue_dir"
	bash "$src_dir/lib/get.sh" \
		0 "$queue_dir/connections" 10 current "$queue_dir/manual" "$queue_repo" \
		"$reasons_file"
)
! grep -Fxq alpha <<<"$deferred_candidates" || {
	echo "Deferred automatic owners must not consume queue slots" >&2
	exit 1
}
grep -Fxq beta <<<"$deferred_candidates" || {
	echo "Available connection owners must remain eligible" >&2
	exit 1
}
grep -Fxq $'beta\tconnection' "$reasons_file" || {
	echo "Owner queue reasons must identify connection candidates" >&2
	exit 1
}
printf '%s\n' 1/alpha >"$queue_dir/manual"
manual_retry_candidates=$(
	cd "$queue_dir"
	bash "$src_dir/lib/get.sh" \
		0 "$queue_dir/connections" 10 current "$queue_dir/manual" "$queue_repo" \
		"$reasons_file"
)
grep -Eq '(^|/)alpha$' <<<"$manual_retry_candidates" || {
	echo "Manual owner requests must override automatic retry backoff" >&2
	exit 1
}
grep -Fxq $'alpha\tmanual' "$reasons_file" || {
	echo "Owner queue reasons must identify manual overrides" >&2
	exit 1
}

mkdir -p "$queue_repo/kept-owner/repo" "$queue_repo/deleted-owner/repo"
printf '%s\n' '[]' >"$queue_repo/kept-owner/repo/.json"
printf '%s\n' '[]' >"$queue_repo/deleted-owner/repo/.json"
git -C "$queue_repo" add .
git -C "$queue_repo" commit -qm owners
rm -rf "$queue_repo/deleted-owner"
git -C "$queue_repo" add -A
git -C "$queue_repo" commit -qm delete-owner
: >"$queue_dir/connections"
: >"$queue_dir/manual"
: >"$queue_dir/owners_deferred"
printf '%s\n' current >"$queue_dir/all_owners_in_db"
history_candidates=$(
	cd "$queue_dir"
	bash "$src_dir/lib/get.sh" \
		0 "$queue_dir/connections" 10 current "$queue_dir/manual" "$queue_repo"
)
grep -Fxq kept-owner <<<"$history_candidates" || {
	echo "Current index owners must remain eligible through history ordering" >&2
	exit 1
}
! grep -Fxq deleted-owner <<<"$history_candidates" || {
	echo "Deleted index owners must not be rediscovered from old commits" >&2
	exit 1
}

check_validation_case() {
	local name=$1
	local extension=$2
	local content=${3-}
	local create_file=${4:-true}
	local expected_output=${5-}
	local expected_exists=${6:-true}
	local file="$workdir/$name$extension"
	local original_file="$workdir/$name.original"
	local output
	local status

	if $create_file; then
		printf '%s' "$content" >"$file"
		cp "$file" "$original_file"
	fi

	set +e
	output=$(bash "$src_dir/index.sh" "$file" 2>&1)
	status=$?
	set -e

	[ "$status" -eq 0 ] || {
		echo "Validation returned status $status for $name" >&2
		exit 1
	}
	[ "$output" = "${expected_output//<file>/$file}" ] || {
		echo "Unexpected validation output for $name" >&2
		printf 'expected: %s\nactual: %s\n' "$expected_output" "${output//$file/<file>}" >&2
		exit 1
	}
	[ -e "$file" ] && actual_exists=true || actual_exists=false
	if [ "$actual_exists" != "$expected_exists" ]; then
		echo "Unexpected validation file retention for $name" >&2
		exit 1
	fi
	if $create_file && $expected_exists && ! cmp -s "$original_file" "$file"; then
		echo "Validation changed file contents for $name" >&2
		exit 1
	fi
}

check_validation_case valid-json .json '{"ok":true}'
check_validation_case sequential-json .json $'{"first":true}\n[1,2,3]\n'
check_validation_case false-json .json 'false' true 'Invalid json: <file>'
check_validation_case invalid-json .json '{"broken":' true 'Invalid json: <file>'
check_validation_case empty-json .json '' true 'Empty file: <file>' false
check_validation_case missing-json .json '' false 'Empty file: <file>' false
check_validation_case valid-xml .xml '<root><value>ok</value></root>'
check_validation_case invalid-xml .xml '<root>' true 'Invalid xml: <file>'
check_validation_case valid-other .data '<root/>'

echo "Python migration tests passed"
