#!/bin/bash

pushd "$1" &>/dev/null || exit 1
git ls-tree -r --name-only "$2" ./ \
	| xargs -r -I filename git log -1 --format='%cs filename' filename \
	| awk -v cutoff="$3" 'cutoff != "" && $1 < cutoff {print}' \
	| sort | grep -oP '(?<= )[^/]+(?=/)' | uniq | awk '{print "0/"$1}'
popd &>/dev/null || exit 1
