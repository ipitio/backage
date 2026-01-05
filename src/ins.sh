#!/bin/bash

base=$1
inserts=$2

[ -s "$base" ] || { cat "$inserts"; return; }
[ -s "$inserts" ] || { cat "$base"; return; }
awk 'BEGIN { srand() }
	NR==FNR { if (!seen_base[$0]++) base[++n] = $0; next }
	{ if (!seen_ins[$0]++) { slot = int(rand() * (n + 1)); ins[slot] = (ins[slot] ? ins[slot] ORS $0 : $0) } }
	END {
		for (i = 0; i <= n; i++) {
			if (ins[i] != "") print ins[i]
			if (i < n) print base[i + 1]
		}
	}
' "$base" "$inserts"
