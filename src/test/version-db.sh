#!/bin/bash

# Test doubles are invoked indirectly by the package adapter.
# shellcheck disable=SC1091,SC2034,SC2317

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
src_dir=${src_dir:?}
workdir=${workdir:?}

test_update_package_delegates_complete_refresh_to_python() {
	local args_file="$workdir/package-refresh-args"
	local output_file="$workdir/package-refresh-output"
	local today=2026-06-26

	(
		cd "$workdir"
		ln -s "$src_dir/lib" lib
		export BKG_SKIP_DEP_VERIFY=1
		source "$src_dir/lib/package.sh"

		owner_id=69664378
		owner=Lazztech
		owner_type=orgs
		fast_out=false
		GITHUB_TOKEN=test-token
		BKG_INDEX_TBL_VER=versions

		check_limit() {
			return 0
		}
		current_batch_first_started() {
			printf '%s\n' "$today"
		}
		bkg_python() {
			printf '%s\n' "$*" >"$args_file"
			printf '%s\n' '{"outcome":"refreshed","package_written":true,"records_written":1,"json_size":100,"xml_size":200}'
		}

		update_package 'npm/Libre-Closet/libre-closet'
	) >"$output_file" 2>&1

	assert_contains "$args_file" "package refresh 69664378 orgs npm Lazztech Libre-Closet libre-closet versions_orgs_npm_Lazztech_Libre-Closet_libre-closet $today true true false"
	assert_contains "$output_file" "Package refresh summary for Lazztech/libre-closet"
	assert_contains "$output_file" "Refreshed Lazztech/libre-closet"
}

test_update_package_preserves_graceful_stop_status() {
	local status=0

	(
		cd "$workdir"
		source "$src_dir/lib/package.sh"
		owner_id=69664378
		owner=Lazztech
		owner_type=orgs
		fast_out=false
		BKG_INDEX_TBL_VER=versions
		check_limit() { return 0; }
		current_batch_first_started() { printf '%s\n' 2026-06-26; }
		bkg_python() { return 3; }
		update_package 'container/Libre-Closet/libre-closet'
	) >/dev/null 2>&1 || status=$?

	[ "$status" -eq 3 ] || fail "Expected package adapter to preserve status 3, got $status"
}

trap cleanup EXIT

run_test test_update_package_delegates_complete_refresh_to_python
run_test test_update_package_preserves_graceful_stop_status

echo "Version DB regression tests passed"
