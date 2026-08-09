# Contributing To `bkg`

This guide covers the current source layout, engineering policies, and local
quality gates.

`bkg`'s Python code targets Python 3.14. Docker is the only host development
prerequisite; Python, uv, and every Python package stay inside the test image
or GitHub Actions. Build the image and run the canonical gate:

```bash
docker build --target test --tag bkg-test .
```

The production-based Docker test target is the canonical development and
quality environment. Its virtual environment lives at `/opt/bkg-test`, outside
the mounted checkout, so the built image can run focused commands against live
source without creating a host `.venv`. The same target is the Build workflow's
pre-publication gate for Python, Debian, Git, zstd, Bash, and ShellCheck.

`.python-version` is the shared Actions and uv source for the supported Python
3.14 feature line. Actions select its latest maintenance release, while the
production image pins that release explicitly. Python patch updates are
deliberate lock, quality, image, and workflow changes. The Debian `bookworm`
base remains fixed so interpreter updates do not also change the operating-system
generation.
`pyproject.toml` similarly requires the uv 0.12 compatibility line. Docker and
Actions tooling may follow its non-breaking patch releases, while a uv minor
upgrade remains an explicit, tested change.

## Python Layout

`src/`bkg`_py` is an intentional src-layout import package. Keep importable code
inside that package rather than moving modules directly under `src`; the extra
directory prevents the repository root and non-package files from being
imported accidentally. The ``bkg`_py` namespace remains an internal
implementation name behind the installed ``bkg`` command, so renaming it would
add compatibility churn without changing the service architecture.

The Python package is organized around three cooperating application domains:

- ``bkg`_py.database` owns the public SQLite repository, settings, persisted
  values, lazy schema work, package plans, owner scans, and version-stage
  writes. Other domains import repository behavior and shared values from
  ``bkg`_py.database`; its internal transaction modules are not cross-domain
  APIs.
- ``bkg`_py.owners` owns owner queue selection, page admission, package refresh,
  scan verification, publication, lifecycle composition, and concurrent owner
  batches. Its package root exposes only the operations needed by discovery
  and the outer run coordinator.
- ``bkg`_py.run` owns top-level phase ordering and application coordination.

Root modules retain the composition and infrastructure boundaries shared by
those domains: `application`, `cli`, `commands`, `config`, `discovery`,
`github`, `publication`, `rendering`, `runtime`, `snapshots`, `state`, and the
package/version update modules.

`cli` and `commands` expose only the supported operational boundary:
`workflow-update`, `run`, `handoff`, `release-tag`, `validate`, and diagnostic
`config`.
Internal services should be called as typed Python APIs instead of gaining a
new command merely to make them testable.

Import database and owner behavior from the domain packages directly. The old
flat module paths are not supported APIs.

Split discovery further only if its authenticated and unauthenticated paths
need several cooperating modules with a small public surface. Avoid catch-all
`utils`, generic service layers, and directory nesting based only on file
count.

## Test Layout

Tests for stable package domains live under matching `src/test/database`,
`src/test/owners`, `src/test/run`, and `src/test/workspace` packages. Keep tests
at `src/test` when they exercise root infrastructure or several domains. A
domain-local support module is appropriate only for setup shared by multiple
modules; keep test package initializers free of fixtures and import side
effects. All quality commands must receive the test directory recursively so a
nested test cannot bypass formatting, linting, or type checking.

## Runtime Scaling

Keep candidate admission, resident work, and materialized work bounded
independently. The owner service hydrates and runs at most one 100-owner wave at
a time. Sparse Git already uses the same 100-path command batch, so changing
this internal wave size requires both a Git operation benchmark and a
stop-latency test.

Batch whole-file state and identity-cache rewrites at natural operation
boundaries. API accounting is the exception: enforce it in memory immediately,
flush after at most 32 responses, and flush when the client closes. Keep schema
inspection lazy and repeat it only when the database file identity changes.

Thread workers are cooperative. Once the drain threshold is exceeded, report
the owner but continue awaiting it so finalization cannot race a live state or
database write. A future hard-kill design requires process-isolated tasks with
process-safe inputs and effects; never attempt to kill a Python thread.

Keep Git branch ownership explicit. The index branch owns its complete sparse
worktree and persisted `.env`; the source branch publication path owns only
top-level `*.txt` files and `README.md`. Add another source path only when the
runtime intentionally generates it. Publication conflicts and push failures
must remain visible, and no-op runs must not create commits.

## Runtime Dependencies

`bkg` is a self-hostable Actions project, so runtime dependencies should stay
small and deliberate. Prefer Python's standard library and existing project
helpers unless a dependency clearly buys correctness, protocol coverage,
performance, or maintainability for behavior that is not specific to `bkg`.

Before adding a runtime dependency, check that it:

- replaces a commodity hard part rather than `bkg`'s own state machine;
- has a small transitive dependency tree and active maintenance;
- is compatible with the Python target, Docker image, and locked `uv`
  environment;
- has acceptable licensing for this repository;
- preserves graceful-stop, atomic-output, exit-status, and self-hosted fork
  behavior;
- is covered by focused tests plus the regression suite.

Good candidates are protocol and format edges where the standard library is not
enough. Poor candidates are wrappers around `bkg`'s SQLite schema, persisted
`.env` state, CLI surface, output layout, or workflow orchestration.

Container version sizing uses GHCR's OCI Distribution API through the existing
pooled HTTP client. It resolves a multi-platform index to `linux/amd64` when
available, otherwise the first runnable platform, and sums compressed layer
descriptor sizes. Public-only runs may use one bounded version-specific request
to `ghcr-badge.egpl.dev` when direct inspection cannot determine a size. That
optional service can include config bytes and choose a different platform, so
its value is deliberately last-resort; unsupported image or tag responses are
cached and an unavailable value remains `-1`. Private-capable runs never expose
package identities to the hosted service.

## Diagnostics

Keep diagnostics Action-friendly: short, one-line messages that explain the
decision or failure without dumping large payloads. Do not add broad `set -x`
or verbose tracing to workflows by default.

Every Python CLI path that returns a non-success status should either emit a
reason on stderr or be called from a shell probe that intentionally suppresses
stderr. Graceful stops must include their reason and continue to return status
`3`. Unexpected internal statuses must be logged and translated before crossing
the process boundary.

When adding a setup, restore, publication, or graceful-stop path, add focused
tests for both the durable state and the diagnostic that would make an Action
failure understandable from the log.

## Commands

After building `bkg-test`, run focused commands against the checkout with the
host user's file permissions. Run the Python tests:

```bash
docker run --rm --user "$(id -u):$(id -g)" --volume "$PWD:/app" \
  --workdir /app bkg-test bash src/test/python.sh
```

Format Python code with Ruff:

```bash
docker run --rm --user "$(id -u):$(id -g)" --volume "$PWD:/app" \
  --workdir /app bkg-test bash src/test/format.sh
```

Run Ruff's formatting and lint checks, Pylint, and strict Pyright without the
regression suites:

```bash
docker run --rm --user "$(id -u):$(id -g)" --volume "$PWD:/app" \
  --workdir /app bkg-test bash src/test/quality.sh
```

Run the canonical final gate, which starts with those quality checks and then
runs ShellCheck and the full compatibility and regression suite:

```bash
docker run --rm --user "$(id -u):$(id -g)" --volume "$PWD:/app" \
  --workdir /app bkg-test bash src/test/regression.sh
```

Rebuild the image to run the same gate from a clean locked environment:

```bash
docker build --target test --tag bkg-test .
```

Print per-test timings while profiling an individual shell suite:

```bash
docker run --rm --user "$(id -u):$(id -g)" --volume "$PWD:/app" \
  --workdir /app --env BKG_TEST_TIMINGS=1 \
  bkg-test bash src/test/runtime.sh
```

Changes that retain or modify shell must also pass ShellCheck through the
relevant regression checks. Runtime changes are not complete until focused
tests, the full regression suite, applicable graceful-stop coverage, and a
representative Action gate have passed.
