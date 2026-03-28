#!/bin/bash
# shellcheck disable=SC1090,SC1091

source_file=$1
function_name=$2
shift 2

[ -n "$source_file" ] || exit 1
[ -n "$function_name" ] || exit 1

export BKG_SKIP_DEP_VERIFY=1
cd "$(dirname "$source_file")/.." || exit 1
source "$source_file"
"$function_name" "$@"