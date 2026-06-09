#!/bin/bash

set -euo pipefail

test_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
src_dir=$(cd "$test_dir/.." && pwd)
workdir=$(mktemp -d)

cleanup() {
	rm -rf "$workdir"
}

trap cleanup EXIT

if ! command -v python3 >/dev/null 2>&1; then
	echo "Python migration tests require python3" >&2
	exit 1
fi

PYTHONPATH="$src_dir" PYTHONDONTWRITEBYTECODE=1 \
	python3 -m unittest discover -s "$test_dir" -p 'test_*.py'

config_json=$(
	cd "$src_dir"
	env -i PATH="$PATH" PYTHONPATH="$src_dir" PYTHONDONTWRITEBYTECODE=1 python3 -m bkg_py config
)

jq -e '
	.github_owner == "ipitio"
	and .github_repo == "backage"
	and (.env_file | endswith("/src/env.env"))
' <<<"$config_json" >/dev/null

branch_config_json=$(
	cd "$src_dir"
	env -i PATH="$PATH" PYTHONPATH="$src_dir" PYTHONDONTWRITEBYTECODE=1 GITHUB_BRANCH=feature python3 -m bkg_py config
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
		python3 -m bkg_py select-owners \
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

legacy_validate_generated_file() {
	local file=$1

	if [ ! -s "$file" ]; then
		echo "Empty file: $file"
		rm -f "$file"
	elif [[ "$file" == *.json ]]; then
		jq -e . "$file" &>/dev/null || echo "Invalid json: $file"
	else
		xmllint --noout "$file" &>/dev/null || echo "Invalid xml: $file"
	fi
}

compare_validation_case() {
	local name=$1
	local extension=$2
	local content=${3-}
	local create_file=${4:-true}
	local legacy_file="$workdir/legacy-$name$extension"
	local python_file="$workdir/python-$name$extension"
	local legacy_output
	local python_output
	local legacy_status
	local python_status

	if $create_file; then
		printf '%s' "$content" >"$legacy_file"
		printf '%s' "$content" >"$python_file"
	fi

	set +e
	legacy_output=$(legacy_validate_generated_file "$legacy_file" 2>&1)
	legacy_status=$?
	python_output=$(bash "$src_dir/index.sh" "$python_file" 2>&1)
	python_status=$?
	set -e

	legacy_output=${legacy_output//"$legacy_file"/<file>}
	python_output=${python_output//"$python_file"/<file>}
	[ "$legacy_status" -eq "$python_status" ] || {
		echo "Validation status mismatch for $name: shell=$legacy_status python=$python_status" >&2
		exit 1
	}
	[ "$legacy_output" = "$python_output" ] || {
		echo "Validation output mismatch for $name" >&2
		printf 'shell: %s\npython: %s\n' "$legacy_output" "$python_output" >&2
		exit 1
	}
	[ -e "$legacy_file" ] && legacy_exists=true || legacy_exists=false
	[ -e "$python_file" ] && python_exists=true || python_exists=false
	[ "$legacy_exists" = "$python_exists" ] || {
		echo "Validation file-retention mismatch for $name" >&2
		exit 1
	}
	if $legacy_exists; then
		cmp -s "$legacy_file" "$python_file" || {
			echo "Validation changed file contents for $name" >&2
			exit 1
		}
	fi
}

compare_validation_case valid-json .json '{"ok":true}'
compare_validation_case sequential-json .json $'{"first":true}\n[1,2,3]\n'
compare_validation_case false-json .json 'false'
compare_validation_case invalid-json .json '{"broken":'
compare_validation_case empty-json .json ''
compare_validation_case missing-json .json '' false
compare_validation_case valid-xml .xml '<root><value>ok</value></root>'
compare_validation_case invalid-xml .xml '<root>'
compare_validation_case valid-other .data '<root/>'

echo "Python migration tests passed"
