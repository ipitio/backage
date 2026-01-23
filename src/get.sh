#!/bin/bash

get_requests() {
	(
		head -n "${2:-1}"
		tail -n "${2:-1}"
	) <"$1"
}

get_remaining() {
	get_requests "$4"
	! grep -qP "\b$3\b" "$1" || echo "0/$3"
	bash ins.sh "$1" <(bash ins.sh <(grep -Fxf "$1" "$2") <(get_requests "$4" "${5:-0}"))
	grep -Fxf "$1" "$2"
}

{
	get_remaining complete_owners "$2" "$4" "$5" | grep -vFxf all_owners_in_db -
	[ "$1" = "0" ] || get_remaining owners_stale "$2" "$4" "$5" "$3"
	get_remaining owners_partially_updated "$2" "$4" "$5" $(((1 + $1) * $3))
	[ "$1" = "1" ] || get_remaining owners_stale "$2" "$4" "$5" $((2 * $3))
} | awk '!seen[$0]++' | head -n $((4 * $3))
