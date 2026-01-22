#!/bin/bash

git -C "$1" log --name-only --pretty=format:%ct -- . | awk '
	/^[0-9]+$/ { ts=$0; next }     # commit timestamp line
	NF==0 { next }                 # skip blanks
	index($0,"/")==0 { next }      # skip root-level files
	{ split($0,a,"/"); d=a[1]; if(!(d in seen)) seen[d]=ts }
	END { for(d in seen) printf "%s %s\n", seen[d], d }
' | sort -n | cut -d' ' -f2- >complete_owners
