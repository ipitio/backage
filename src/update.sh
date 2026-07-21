#!/bin/bash
# Run one repository update through the Python workflow service.
# Usage: src/update.sh [ROOT] [-d DURATION] [-m MODE]

# shellcheck disable=SC1090,SC1091

invocation_directory=$(pwd -P) || exit 1
script_directory=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P) || exit 1
cd "$script_directory" || exit 1
source bkg.sh

bkg_python workflow-update \
    --invocation-directory "$invocation_directory" \
    "$@"
