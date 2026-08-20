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
import {
  dashboardFixture,
  historyFixture,
} from "../../tests/dashboard-fixtures.ts";

test("parses a complete internally consistent dashboard", () => {
  const dashboard = parseDashboard(validDashboard());

  assert.equal(dashboard.inventory.packages, 1_200);
  assert.equal(dashboard.package_types.items[0]?.name, "container");
  assert.equal(dashboard.freshness.buckets[4]?.name, "unknown");
  assert.equal(dashboard.metrics.size.known_packages, 1_000);
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

test("rejects invalid nested fields, dates, and unsafe counts", () => {
  const nestedField = validDashboard();
  const inventory = nestedField.inventory as Record<string, unknown>;
  inventory.surprise = true;
  assert.throws(() => parseDashboard(nestedField), DashboardSchemaError);

  assert.throws(
    () => parseDashboard({ ...validDashboard(), generated_date: "2026-02-30" }),
    DashboardSchemaError,
  );

  const unsafeCount = validDashboard();
  const unsafeInventory = unsafeCount.inventory as Record<string, unknown>;
  unsafeInventory.packages = Number.MAX_SAFE_INTEGER + 1;
  assert.throws(() => parseDashboard(unsafeCount), DashboardSchemaError);
});

test("rejects inconsistent inventory, partitions, and coverage", () => {
  const badInventory = validDashboard();
  const inventory = badInventory.inventory as Record<string, unknown>;
  inventory.owners = 400;
  assert.throws(
    () => parseDashboard(badInventory),
    /inconsistent catalog counts/,
  );

  const badPartition = validDashboard();
  const packageTypes = badPartition.package_types as Record<string, unknown>;
  packageTypes.other_packages = 51;
  packageTypes.other_coverage_basis_points = 425;
  assert.throws(() => parseDashboard(badPartition), /does not partition/);

  const badCoverage = validDashboard();
  const metrics = badCoverage.metrics as Record<string, unknown>;
  const size = metrics.size as Record<string, unknown>;
  size.coverage_basis_points = 8_332;
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

  assert.equal(history.samples.length, 3);
  assert.equal(history.samples[0]?.packages, 1_180);
  assert.equal(history.samples[2]?.downloads_known_packages, 1_100);

  const fetcher = (() =>
    Promise.resolve(
      new Response(JSON.stringify(validHistory())),
    )) as typeof fetch;
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
    inconsistentSamples[0].size_known_packages = 1_181;
  }
  assert.throws(
    () => parseDashboardHistory(inconsistent, dashboard),
    /inconsistent catalog counts/,
  );

  const mismatched = validHistory();
  const mismatchedSamples = mismatched.samples as Array<
    Record<string, unknown>
  >;
  if (mismatchedSamples[2] !== undefined) {
    mismatchedSamples[2].packages = 1_199;
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
  return dashboardFixture("2026-08-11") as Record<string, unknown>;
}

function validHistory(): Record<string, unknown> {
  return historyFixture("2026-08-11") as Record<string, unknown>;
}
