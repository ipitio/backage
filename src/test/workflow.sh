#!/bin/bash

set -euo pipefail

test_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd "$test_dir/../.." && pwd)

for workflow in manual stop update; do
    workflow_file="$repo_dir/.github/workflows/$workflow.yml"
    grep -Fq 'python-version-file: ".python-version"' "$workflow_file" || {
        echo "$workflow workflow does not use the shared Python version file" >&2
        exit 1
    }
    grep -Fq 'allow-prereleases: true' "$workflow_file" || {
        echo "$workflow workflow does not allow the selected Python prerelease" >&2
        exit 1
    }
done

if grep -R -Eq 'actions/checkout@(main|v[1-6])' \
    "$repo_dir/.github/workflows"; then
    echo "A workflow does not use the supported checkout major" >&2
    exit 1
fi
if grep -R -Eq 'actions/setup-python@v[1-6]' \
    "$repo_dir/.github/workflows"; then
    echo "A workflow does not use the supported setup-python major" >&2
    exit 1
fi

dockerfile="$repo_dir/Dockerfile"
build_workflow="$repo_dir/.github/workflows/publish.yml"
grep -Fq "FROM python-base AS test" "$dockerfile" || {
    echo "Dockerfile does not define the production-based test target" >&2
    exit 1
}
grep -Fq "UV_PROJECT_ENVIRONMENT=/opt/bkg-test" "$dockerfile" || {
    echo "Docker test environment is hidden by a mounted checkout" >&2
    exit 1
}
grep -Fq "RUN bash src/test/regression.sh" "$dockerfile" || {
    echo "Docker test target does not run the canonical regression gate" >&2
    exit 1
}
grep -Fq "target: test" "$build_workflow" || {
    echo "Build workflow does not execute the Docker test target" >&2
    exit 1
}
awk '
    /target: test/ { test_line = NR }
    /push: true/ { push_line = NR }
    END { exit !(test_line && push_line && test_line < push_line) }
' "$build_workflow" || {
    echo "Build workflow does not test before publishing the image" >&2
    exit 1
}
if grep -Fq "run: bash src/test/regression.sh" "$build_workflow"; then
    echo "Build workflow duplicates the full gate outside its Docker target" >&2
    exit 1
fi
if grep -Eqs 'uv (sync|run)' \
    "$repo_dir/src/test/format.sh" \
    "$repo_dir/src/test/python.sh" \
    "$repo_dir/src/test/quality.sh"; then
    echo "Test scripts try to manage a host Python environment" >&2
    exit 1
fi

grep -Fq 'required-version = "==0.12.*"' "$repo_dir/pyproject.toml" || {
    echo "Project does not declare the supported uv compatibility line" >&2
    exit 1
}
if grep -Eq '^[[:space:]]+version:[[:space:]]+"?0\.[0-9]+\.[0-9]+' \
    "$build_workflow"; then
    echo "Build workflow pins uv more narrowly than the project policy" >&2
    exit 1
fi

for workflow in manual update; do
    workflow_file="$repo_dir/.github/workflows/$workflow.yml"
    grep -Fq "bkg workflow-update -C /app -D \"\$run_date\"" "$workflow_file" || {
        echo "$workflow workflow does not pass one date to the Python entrypoint" >&2
        exit 1
    }
    grep -Fq "RUN_DATE: \${{ steps.update.outputs.run_date }}" "$workflow_file" || {
        echo "$workflow workflow does not reuse the run date for publication" >&2
        exit 1
    }
    grep -Fq "python -m bkg_py release-tag -D \"\$RUN_DATE\"" "$workflow_file" || {
        echo "$workflow workflow does not use the shared release-tag policy" >&2
        exit 1
    }
    grep -Fq "tag: \"\${{ steps.date.outputs.tag }}\"" "$workflow_file" || {
        echo "$workflow workflow does not publish the complete shared tag" >&2
        exit 1
    }
    grep -Fq '/var/run/docker.sock:/var/run/docker.sock' "$workflow_file" || {
        echo "$workflow workflow does not expose its Docker daemon to the fallback" >&2
        exit 1
    }
    grep -Fq 'BKG_DOCKER_SIZE_FALLBACK=true' "$workflow_file" || {
        echo "$workflow workflow does not opt in to Docker size inspection" >&2
        exit 1
    }
    grep -Fq "ghcr.io/\$GITHUB_OWNER/\$GITHUB_REPO:master" "$workflow_file" || {
        echo "$workflow workflow does not run its repository-owned image" >&2
        exit 1
    }
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

grep -Fq "\${{ github.repository }}-database-publication" \
    "$repo_dir/.github/workflows/vacuum.yml" || {
    echo "vacuum does not serialize with database publication" >&2
    exit 1
}

grep -Fq 'bkg vacuum-releases' "$repo_dir/.github/workflows/vacuum.yml" || {
    echo "vacuum does not use selective database release retention" >&2
    exit 1
}

if grep -Fq 'delete-old-releases' "$repo_dir/.github/workflows/vacuum.yml"; then
    echo "vacuum still uses generic semver release cleanup" >&2
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

echo "Workflow regression tests passed"
