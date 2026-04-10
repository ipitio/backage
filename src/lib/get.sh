#!/bin/bash

get_phase_started_at() {
    date -u +%s
}

log_get_phase() {
    local phase=$1
    local started_at=${2:-0}
    local elapsed=0

    ((started_at > 0)) || return 0
    elapsed=$(( $(date -u +%s) - started_at ))
    echo "Owner selection phase '$phase' completed in ${elapsed}s" >&2
}

get_requests() {
	head -n "${2:-1}" <"$1"
	tail -n "${2:-1}" <"$1"
}

insert_into(){
	awk '
BEGIN { srand() }
NR==FNR { base[++n] = $0; next }
	{ ins[++m]  = $0 }
END {
    if (n == 0) {
        for (i = 1; i <= m; i++) print ins[i]
    } else if (m == 0) {
        for (i = 1; i <= n; i++) print base[i]
    } else {
        for (i = 1; i <= m; i++) {
            slot = int(rand() * (n + 1))
            buckets[slot] = (slot in buckets ? buckets[slot] ORS ins[i] : ins[i])
        }

        for (i = 0; i <= n; i++) {
            if (i in buckets) print buckets[i]
            if (i < n) print base[i + 1]
        }
    }
}
' "$1" "$2"
}

get_remaining() {
	get_requests "$4"
    ! grep -Fxq "$3" "$1" || echo "0/$3"
	insert_into "$1" <(insert_into <(grep -Fxf "$1" "$2") <(get_requests "$4" "${5:-0}"))
	grep -Fxf "$1" "$2"
}

get_discovered() {
    {
        get_requests "$3"
        [ -n "$2" ] && echo "$2"
        cat "$1"
    } | awk 'NF && !seen[$0]++'
}

get_owners(){
    local phase_started_at=0

    phase_started_at=$(get_phase_started_at)
    git -C "$6" log --name-only --pretty=format:%ct -- . 2>/dev/null | awk '
/^[0-9]+$/ { ts=$0; next }     # commit timestamp line
NF==0 { next }                 # skip blanks
index($0,"/")==0 { next }      # skip root-level files
{ split($0,a,"/"); d=a[1]; if(!(d in seen)) seen[d]=ts }
END { for(d in seen) printf "%s %s\n", seen[d], d }
' | sort -n | cut -d' ' -f2- >complete_owners
    log_get_phase "scan-index-history" "$phase_started_at"

    phase_started_at=$(get_phase_started_at)
    get_discovered "$2" "$4" "$5" | grep -vFxf all_owners_in_db -
	get_remaining complete_owners "$2" "$4" "$5" | grep -vFxf all_owners_in_db -
	rm -f complete_owners
	[ "$1" = "0" ] || get_remaining owners_stale "$2" "$4" "$5"
	insert_into <(get_remaining owners_partially_updated "$2" "$4" "$5" "$3") <(get_remaining owners_stale "$2" "$4" "$5")
    log_get_phase "assemble-owner-candidates" "$phase_started_at"
}

get_owners "$1" "$2" "$3" "$4" "$5" "$6" | awk '!seen[$0]++' | head -n $((2 * $3))
