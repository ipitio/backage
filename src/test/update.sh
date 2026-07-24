#!/bin/bash

set -euo pipefail

test_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
launcher="$test_dir/../update.sh"
repo_dir=$(cd "$test_dir/../.." && pwd)

for workflow in manual publish stop update; do
    workflow_file="$repo_dir/.github/workflows/$workflow.yml"
    grep -Fq 'python-version-file: ".python-version"' "$workflow_file" || {
        echo "$workflow workflow does not use the shared Python version file" >&2
        exit 1
    }
done

grep -Fq -- '-m bkg_py workflow-update' "$launcher" || {
    echo "Update launcher does not delegate to the Python workflow service" >&2
    exit 1
}

if grep -Eq '(^|[[:space:]])source[[:space:]]' "$launcher"; then
    echo "Update launcher still sources the removed shell implementation" >&2
    exit 1
fi

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
    grep -Fq 'python -m bkg_py handoff' "$workflow_file" || {
        echo "$workflow workflow does not use Python handoff control" >&2
        exit 1
    }
done

if grep -Eq '(^|[[:space:]])jq([[:space:]]|$)' \
    "$repo_dir/.github/workflows/update.yml"; then
    echo "Update workflow still requires jq for freshness parsing" >&2
    exit 1
fi

stop_workflow="$repo_dir/.github/workflows/stop.yml"
grep -Fq "workflow_dispatch:" "$stop_workflow" || {
    echo "Stop workflow is not manually dispatchable" >&2
    exit 1
}
grep -Fq 'python -m bkg_py handoff request' "$stop_workflow" || {
    echo "Stop workflow does not request a graceful Python handoff" >&2
    exit 1
}
if grep -Eq '^[[:space:]]+concurrency:' "$stop_workflow"; then
    echo "Stop workflow must not wait for publication concurrency" >&2
    exit 1
fi
if grep -Eq '^  update:' "$stop_workflow"; then
    echo "Stop workflow must not queue a replacement update" >&2
    exit 1
fi

if grep -R -Eq 'src/lib/handoff\.sh|source src/lib/handoff\.sh' \
    "$repo_dir/.github/workflows"; then
    echo "A workflow still invokes the removed handoff adapter" >&2
    exit 1
fi

grep -Fxq '.venv' "$repo_dir/.dockerignore" || {
    echo "Docker context does not exclude the project virtual environment" >&2
    exit 1
}

grep -Fq 'Edit this template, not README.md.' \
    "$repo_dir/src/templates/.README.md" || {
    echo "README template does not identify itself as the generated source" >&2
    exit 1
}

echo "Workflow and compatibility launcher regression tests passed"
