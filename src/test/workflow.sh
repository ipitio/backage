#!/bin/bash

set -euo pipefail

test_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd "$test_dir/../.." && pwd)

for workflow in manual stop sync update; do
    workflow_file="$repo_dir/.github/workflows/$workflow.yml"
    grep -Fq 'python-version-file: ".python-version"' "$workflow_file" || {
        echo "$workflow workflow does not use the shared Python version file" >&2
        exit 1
    }
    if grep -Fq 'allow-prereleases: true' "$workflow_file"; then
        echo "$workflow workflow unexpectedly allows Python prereleases" >&2
        exit 1
    fi
done

grep -Fxq '3.14' "$repo_dir/.python-version" || {
    echo "Shared Python feature line exceeds Dependabot graph support" >&2
    exit 1
}
grep -Fq 'requires-python = ">=3.14,<3.15"' "$repo_dir/pyproject.toml" || {
    echo "Project Python range exceeds Dependabot graph support" >&2
    exit 1
}

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
manual_workflow="$repo_dir/.github/workflows/manual.yml"
sync_workflow="$repo_dir/.github/workflows/sync.yml"
grep -Fq "FROM python-base AS test" "$dockerfile" || {
    echo "Dockerfile does not define the production-based test target" >&2
    exit 1
}
grep -Fq "UV_PROJECT_ENVIRONMENT=/opt/bkg-test" "$dockerfile" || {
    echo "Docker test environment is hidden by a mounted checkout" >&2
    exit 1
}
grep -Fq "RUN test -f /tmp/.browser-tests-passed" "$dockerfile" || {
    echo "Docker test target does not consume the browser-test result" >&2
    exit 1
}
grep -Fq "&& bash src/test/regression.sh" "$dockerfile" || {
    echo "Docker test target does not run the canonical regression gate" >&2
    exit 1
}
grep -Fq "ARG NODE_VERSION=24" "$dockerfile" || {
    echo "Dockerfile does not declare the supported Node feature line" >&2
    exit 1
}
grep -Fq 'packageManager": "npm@11.17.0"' \
    "$repo_dir/site/package.json" || {
    echo "Site project does not pin the supported npm release" >&2
    exit 1
}
grep -Fq "RUN npm ci --strict-allow-scripts" "$dockerfile" || {
    echo "Docker site stage does not enforce its install-script allowlist" >&2
    exit 1
}
grep -Fq "RUN npm test && npm run check && npm run build" "$dockerfile" || {
    echo "Docker site stage does not test, check, and build the locked Astro source" >&2
    exit 1
}
grep -Fq "FROM site-dependencies AS site-browser-runtime" "$dockerfile" || {
    echo "Dockerfile does not cache the browser runtime with site dependencies" >&2
    exit 1
}
grep -Fq "playwright install --with-deps --only-shell chromium" "$dockerfile" || {
    echo "Docker browser-test stage does not install its locked Chromium runtime" >&2
    exit 1
}
grep -Fq "FROM site-browser-runtime AS site-browser-test" "$dockerfile" || {
    echo "Dockerfile does not isolate browser tests from their runtime" >&2
    exit 1
}
grep -Fq "COPY src/img/logo-b.webp ./dist/logo-b.webp" "$dockerfile" || {
    echo "Docker browser-test stage does not include the published brand mark" >&2
    exit 1
}
grep -Fq "COPY src/img/logo.ico ./dist/favicon.ico" "$dockerfile" || {
    echo "Docker browser-test stage does not include the published favicon" >&2
    exit 1
}
grep -Fq "RUN npm run test:browser && touch /site/.browser-tests-passed" "$dockerfile" || {
    echo "Docker browser-test stage does not run the browser smoke tests" >&2
    exit 1
}
grep -Fq "COPY --from=site-browser-test /site/.browser-tests-passed /tmp/" "$dockerfile" || {
    echo "Docker test target does not require the browser-test result" >&2
    exit 1
}
grep -Fq "COPY --from=site-build /site/dist /opt/bkg/share/backage/site" \
    "$dockerfile" || {
    echo "Production environment does not package the built site shell" >&2
    exit 1
}
grep -Fq "&& ! command -v node" "$dockerfile" || {
    echo "Production image does not verify that Node is absent" >&2
    exit 1
}
grep -Fq "bash \"\$test_dir/site.sh\"" "$repo_dir/src/test/quality.sh" || {
    echo "Canonical quality checks do not include the Astro site" >&2
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
    grep -Fq "release_tag=\$(docker run --rm ghcr.io/\$GITHUB_OWNER/\$GITHUB_REPO:master bkg release-tag -D \"\$RUN_DATE\")" \
        "$workflow_file" || {
        echo "$workflow workflow does not run release-tag from its image" >&2
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
    grep -Fq "docker run --rm \\" "$workflow_file" || {
        echo "$workflow workflow does not remove its update container" >&2
        exit 1
    }
    if grep -Fq -- '--env-file <(' "$workflow_file"; then
        echo "$workflow workflow still forwards a broad environment prefix" >&2
        exit 1
    fi
    grep -Fq "ghcr.io/\$GITHUB_OWNER/\$GITHUB_REPO:master" "$workflow_file" || {
        echo "$workflow workflow does not run its repository-owned image" >&2
        exit 1
    }
    grep -Fq 'python -m bkg_py handoff' "$workflow_file" || {
        echo "$workflow workflow does not use Python handoff control" >&2
        exit 1
    }
done

if grep -Fq 'pull_request:' "$manual_workflow"; then
    echo "Manual workflow still runs after unrelated pull requests close" >&2
    exit 1
fi
grep -Fq 'branches:' "$manual_workflow" || {
    echo "Manual workflow does not limit completed builds to a source branch" >&2
    exit 1
}
grep -Fq '      - master' "$manual_workflow" || {
    echo "Manual workflow can run for a build that did not publish the master image" >&2
    exit 1
}

if grep -R -Fq 'PYTHONPATH=src python -m bkg_py release-tag' \
    "$repo_dir/.github/workflows"; then
    echo "A workflow runs release-tag with the host Python" >&2
    exit 1
fi

if grep -Eq '(^|[[:space:]])jq([[:space:]]|$)' \
    "$repo_dir/.github/workflows/update.yml"; then
    echo "Update workflow still requires jq for freshness parsing" >&2
    exit 1
fi

grep -Fq 'github.event.repository.fork' "$sync_workflow" || {
    echo "Upstream sync is not limited to forks" >&2
    exit 1
}
grep -Fq 'workflow_dispatch:' "$sync_workflow" || {
    echo "Upstream sync cannot be requested manually" >&2
    exit 1
}
grep -Fq 'cron: "23 */6 * * *"' "$sync_workflow" || {
    echo "Upstream sync does not use the bounded six-hour schedule" >&2
    exit 1
}
grep -Fq "\${{ github.repository }}-database-publication" "$sync_workflow" || {
    echo "Upstream sync does not serialize with database publication" >&2
    exit 1
}
grep -Fq 'actions: write' "$sync_workflow" || {
    echo "Upstream sync cannot dispatch the synchronized build" >&2
    exit 1
}
grep -Fq 'contents: write' "$sync_workflow" || {
    echo "Upstream sync cannot publish its merge" >&2
    exit 1
}
grep -Fq 'fetch-depth: 0' "$sync_workflow" || {
    echo "Upstream sync does not fetch the history needed for a real merge" >&2
    exit 1
}
grep -Fq '[.source.clone_url, .source.default_branch]' "$sync_workflow" || {
    echo "Upstream sync does not resolve the fork network's canonical source" >&2
    exit 1
}
grep -Fq 'workflow-sync-fork' "$sync_workflow" || {
    echo "Upstream sync bypasses the tested fork merge policy" >&2
    exit 1
}
grep -Fq "git push origin \"HEAD:refs/heads/\$FORK_BRANCH\"" "$sync_workflow" || {
    echo "Upstream sync does not publish a normal branch update" >&2
    exit 1
}
grep -Fq "gh workflow run publish.yml --ref \"\$FORK_BRANCH\"" "$sync_workflow" || {
    echo "Upstream sync does not explicitly dispatch the suppressed build" >&2
    exit 1
}
if grep -Eq 'git push .* (-f|--force)' "$sync_workflow"; then
    echo "Upstream sync must not force-push a fork branch" >&2
    exit 1
fi
awk '
    /git push origin/ { push_line = NR }
    /gh workflow run publish.yml/ { build_line = NR }
    END { exit !(push_line && build_line && push_line < build_line) }
' "$sync_workflow" || {
    echo "Upstream sync does not publish source before dispatching its build" >&2
    exit 1
}

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
grep -Fxq '**/node_modules' "$repo_dir/.dockerignore" || {
    echo "Docker context does not exclude nested frontend dependencies" >&2
    exit 1
}

grep -Fq 'Edit this template, not README.md.' \
    "$repo_dir/src/templates/.README.md" || {
    echo "README template does not identify itself as the generated source" >&2
    exit 1
}
if grep -Fq '/pkgs/container/backage' "$repo_dir/src/templates/.README.md"; then
    echo "README template hard-codes Main's container package name" >&2
    exit 1
fi

echo "Workflow regression tests passed"
