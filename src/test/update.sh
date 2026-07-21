#!/bin/bash

set -euo pipefail

test_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
launcher="$test_dir/../update.sh"

grep -Fq 'bkg_python workflow-update' "$launcher" || {
    echo "Update launcher does not delegate to the Python workflow service" >&2
    exit 1
}

for migrated_operation in \
    capture_workflow_handoff_baseline \
    prepare-index \
    restore_startup_database_snapshot_if_needed \
    publish-update; do
    if grep -Fq "$migrated_operation" "$launcher"; then
        echo "Update launcher still owns $migrated_operation" >&2
        exit 1
    fi
done

echo "Update launcher regression tests passed"
