import assert from "node:assert/strict";
import test from "node:test";

import {
  DashboardLoadError,
  DashboardSchemaError,
  formatBytes,
  formatCoverage,
  formatPublicationDate,
  loadDashboard,
  loadDashboardHistory,
  parseDashboard,
  parseDashboardHistory,
  publicationState,
} from "./dashboard.ts";

test("parses a complete internally consistent dashboard", () => {
  const dashboard = parseDashboard(validDashboard());

  assert.equal(dashboard.inventory.packages, 10);
  assert.equal(dashboard.package_types.items[0]?.name, "container");
  assert.equal(dashboard.freshness.buckets[4]?.name, "unknown");
  assert.equal(dashboard.metrics.size.known_packages, 8);
});

test("rejects unknown schema fields and versions", () => {
  assert.throws(
    () => parseDashboard({ ...validDashboard(), surprise: true }),
    DashboardSchemaError,
  );
  assert.throws(
    () => parseDashboard({ ...validDashboard(), schema_version: 2 }),
    /unsupported dashboard schema version 2/,
  );
});

test("rejects inconsistent inventory, partitions, and coverage", () => {
  const badInventory = validDashboard();
  const inventory = badInventory.inventory as Record<string, unknown>;
  inventory.owners = 8;
  assert.throws(() => parseDashboard(badInventory), /inconsistent catalog counts/);

  const badPartition = validDashboard();
  const packageTypes = badPartition.package_types as Record<string, unknown>;
  packageTypes.other_packages = 2;
  packageTypes.other_coverage_basis_points = 2_000;
  assert.throws(() => parseDashboard(badPartition), /does not partition/);

  const badCoverage = validDashboard();
  const metrics = badCoverage.metrics as Record<string, unknown>;
  const size = metrics.size as Record<string, unknown>;
  size.coverage_basis_points = 7_999;
  assert.throws(() => parseDashboard(badCoverage), /coverage does not match/);
});

test("classifies current, stale, and future publication dates", () => {
  const now = new Date("2026-08-11T23:59:59Z");

  assert.equal(publicationState("2026-08-10", now), "current");
  assert.equal(publicationState("2026-08-09", now), "stale");
  assert.equal(publicationState("2026-08-12", now), "future");
});

test("loads a bounded dashboard response", async () => {
  const fetcher = (() =>
    Promise.resolve(
      new Response(JSON.stringify(validDashboard()), {
        headers: { "content-type": "application/json" },
      }),
    )) as typeof fetch;

  const dashboard = await loadDashboard("./dashboard.json", { fetcher });

  assert.equal(dashboard.generated_date, "2026-08-11");
});

test("parses bounded history that matches the current projection", async () => {
  const dashboard = parseDashboard(validDashboard());
  const history = parseDashboardHistory(validHistory(), dashboard);

  assert.equal(history.samples.length, 2);
  assert.equal(history.samples[0]?.packages, 9);
  assert.equal(history.samples[1]?.downloads_known_packages, 9);

  const fetcher = (() =>
    Promise.resolve(new Response(JSON.stringify(validHistory())))) as typeof fetch;
  const loaded = await loadDashboardHistory(
    "./dashboard-history.json",
    dashboard,
    { fetcher },
  );
  assert.equal(loaded.samples.at(-1)?.date, "2026-08-11");
});

test("rejects history that is stale, unordered, or internally inconsistent", () => {
  const dashboard = parseDashboard(validDashboard());
  const nextDay = parseDashboard({
    ...validDashboard(),
    generated_date: "2026-08-12",
  });
  assert.throws(
    () => parseDashboardHistory(validHistory(), nextDay),
    /does not match the current publication date/,
  );

  const unordered = validHistory();
  const unorderedSamples = unordered.samples as Array<Record<string, unknown>>;
  unorderedSamples[1] = { ...unorderedSamples[0] };
  assert.throws(
    () => parseDashboardHistory(unordered, dashboard),
    /increasing unique dates/,
  );

  const inconsistent = validHistory();
  const inconsistentSamples = inconsistent.samples as Array<
    Record<string, unknown>
  >;
  if (inconsistentSamples[0] !== undefined) {
    inconsistentSamples[0].size_known_packages = 10;
  }
  assert.throws(
    () => parseDashboardHistory(inconsistent, dashboard),
    /inconsistent catalog counts/,
  );

  const mismatched = validHistory();
  const mismatchedSamples = mismatched.samples as Array<Record<string, unknown>>;
  if (mismatchedSamples[1] !== undefined) {
    mismatchedSamples[1].packages = 9;
  }
  assert.throws(
    () => parseDashboardHistory(mismatched, dashboard),
    /does not match the current projection/,
  );
});

test("rejects declared and streamed responses above the byte limit", async () => {
  const declaredFetcher = (() =>
    Promise.resolve(
      new Response("{}", { headers: { "content-length": "11" } }),
    )) as typeof fetch;
  await assert.rejects(
    loadDashboard("./dashboard.json", {
      fetcher: declaredFetcher,
      maxBytes: 10,
    }),
    (error: unknown) =>
      error instanceof DashboardLoadError && error.kind === "invalid",
  );

  const streamedFetcher = (() =>
    Promise.resolve(new Response("12345678901"))) as typeof fetch;
  await assert.rejects(
    loadDashboard("./dashboard.json", {
      fetcher: streamedFetcher,
      maxBytes: 10,
    }),
    /dashboard response is too large/,
  );
});

test("formats dashboard values consistently", () => {
  assert.equal(formatCoverage(7_550), "75.5%");
  assert.equal(formatBytes(1_536), "1.5 KiB");
  assert.equal(formatPublicationDate("2026-08-11"), "August 11, 2026");
});

function validDashboard(): Record<string, unknown> {
  return {
    schema_version: 1,
    generated_date: "2026-08-11",
    inventory: {
      owners: 4,
      repositories: 7,
      packages: 10,
      resolved_packages: 9,
    },
    package_types: {
      unit: "packages",
      denominator: "catalog_packages",
      limit: 16,
      items: [
        { name: "container", packages: 6, coverage_basis_points: 6_000 },
        { name: "npm", packages: 3, coverage_basis_points: 3_000 },
      ],
      other_packages: 1,
      other_coverage_basis_points: 1_000,
    },
    freshness: {
      unit: "packages",
      denominator: "catalog_packages",
      unknown_treatment: "missing_invalid_or_future_observed_date",
      buckets: [
        { name: "today", packages: 5, coverage_basis_points: 5_000 },
        { name: "days_1_7", packages: 2, coverage_basis_points: 2_000 },
        { name: "days_8_30", packages: 1, coverage_basis_points: 1_000 },
        { name: "days_31_plus", packages: 1, coverage_basis_points: 1_000 },
        { name: "unknown", packages: 1, coverage_basis_points: 1_000 },
      ],
    },
    metrics: {
      size: metric("bytes", 8, 2, 8_000, 1_536),
      downloads_day: metric("downloads", 7, 3, 7_000, 70),
      downloads_week: metric("downloads", 6, 4, 6_000, 420),
      downloads_month: metric("downloads", 5, 5, 5_000, 1_500),
      downloads_total: metric("downloads", 9, 1, 9_000, 20_000),
    },
    history: {
      path: "dashboard-history.json",
      schema_version: 1,
      retention_days: 180,
    },
  };
}

function validHistory(): Record<string, unknown> {
  return {
    schema_version: 1,
    retention_days: 180,
    samples: [
      {
        date: "2026-08-10",
        owners: 4,
        repositories: 7,
        packages: 9,
        size_known_packages: 7,
        downloads_known_packages: 8,
      },
      {
        date: "2026-08-11",
        owners: 4,
        repositories: 7,
        packages: 10,
        size_known_packages: 8,
        downloads_known_packages: 9,
      },
    ],
  };
}

function metric(
  unit: "bytes" | "downloads",
  knownPackages: number,
  unknownPackages: number,
  coverageBasisPoints: number,
  value: number,
): Record<string, unknown> {
  return {
    unit,
    denominator: "catalog_packages",
    unknown_treatment: "negative_or_missing_current_value",
    known_packages: knownPackages,
    unknown_packages: unknownPackages,
    coverage_basis_points: coverageBasisPoints,
    value,
  };
}
