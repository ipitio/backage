#!/bin/bash

# shellcheck disable=SC2034

test_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
src_dir=$(cd "$test_dir/.." && pwd)
workdir=$(mktemp -d)

cleanup() {
	rm -rf "$workdir"
}

fail() {
	echo "$1" >&2
	exit 1
}

assert_file_exists() {
	[ -f "$1" ] || fail "Expected file to exist: $1"
}

assert_contains() {
	grep -Fq "$2" "$1" || fail "Expected $1 to contain $2"
}

assert_not_contains() {
	! grep -Fq "$2" "$1" || fail "Expected $1 to not contain $2"
}