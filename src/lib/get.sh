#!/bin/bash

get_requests() {
	(
		head -n "${2:-1}"
		tail -n "${2:-1}"
	) <"$1"
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
	! grep -qP "\b$3\b" "$1" || echo "0/$3"
	insert_into "$1" <(insert_into <(grep -Fxf "$1" "$2") <(get_requests "$4" "${5:-0}"))
	grep -Fxf "$1" "$2"
}

get_owners(){
	git -C "$1" log --name-only --pretty=format:%ct -- . | awk '
/^[0-9]+$/ { ts=$0; next }     # commit timestamp line
NF==0 { next }                 # skip blanks
index($0,"/")==0 { next }      # skip root-level files
{ split($0,a,"/"); d=a[1]; if(!(d in seen)) seen[d]=ts }
END { for(d in seen) printf "%s %s\n", seen[d], d }
' | sort -n | cut -d' ' -f2- >complete_owners
	get_remaining complete_owners "$2" "$4" "$5" | grep -vFxf all_owners_in_db -
	rm -f complete_owners
	[ "$1" = "0" ] || get_remaining owners_stale "$2" "$4" "$5"
	get_remaining owners_partially_updated "$2" "$4" "$5" "$3"
	[ "$1" = "1" ] || get_remaining owners_stale "$2" "$4" "$5"
}

get_owners "$1" "$2" "$3" "$4" "$5" 2>/dev/null | awk '!seen[$0]++' | head -n $((3 * $3))
