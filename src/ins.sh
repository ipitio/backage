#!/bin/bash

base=$1
inserts=$2

awk '
BEGIN { srand() }
NR==FNR { base[++n] = $0; next }
	{ ins[++m]  = $0 }
END {
	if (n == 0) { for (i = 1; i <= m; i++) print ins[i]; next }
	if (m == 0) { for (i = 1; i <= n; i++) print base[i]; next }

	for (i = 1; i <= m; i++) {
		slot = int(rand() * (n + 1))
		buckets[slot] = (slot in buckets ? buckets[slot] ORS ins[i] : ins[i])
	}

	for (i = 0; i <= n; i++) {
		if (i in buckets) print buckets[i]
		if (i < n) print base[i + 1]
	}
}
' "$base" "$inserts"
