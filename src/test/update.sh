#!/bin/bash

set -euo pipefail

test_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
launcher="$test_dir/../update.sh"
repo_dir=$(cd "$test_dir/../.." && pwd)

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

for workflow in manual update; do
    workflow_file="$repo_dir/.github/workflows/$workflow.yml"
    grep -Fq 'bkg workflow-update bkg --invocation-directory /app' "$workflow_file" || {
        echo "$workflow workflow does not call the installed Python entrypoint" >&2
        exit 1
    }
    if grep -Fq 'src/update.sh' "$workflow_file"; then
        echo "$workflow workflow still calls the compatibility launcher" >&2
        exit 1
    fi
done

grep -Fxq '.venv' "$repo_dir/.dockerignore" || {
    echo "Docker context does not exclude the project virtual environment" >&2
    exit 1
}

echo "Update launcher regression tests passed"
