import assert from "node:assert/strict";
import test from "node:test";

import {
  DashboardSchemaError,
  type DashboardHistorySample,
} from "./dashboard.ts";
import { formatChange, selectHistorySamples } from "./history.ts";

function sample(date: string, packages = 10): DashboardHistorySample {
  return {
    date,
    packages,
    owners: 1,
    repositories: 2,
    size_known_packages: 0,
    downloads_known_packages: 0,
  };
}

test("history periods use calendar days, not a number of observations", () => {
  const samples = [
    sample("2026-08-01"),
    sample("2026-08-02"),
    sample("2026-08-24"),
    sample("2026-08-25"),
    sample("2026-08-31"),
  ];
  assert.deepEqual(
    selectHistorySamples(samples, "7").map((item) => item.date),
    ["2026-08-25", "2026-08-31"],
  );
  assert.deepEqual(
    selectHistorySamples(samples, "30").map((item) => item.date),
    ["2026-08-02", "2026-08-24", "2026-08-25", "2026-08-31"],
  );
  assert.deepEqual(selectHistorySamples(samples, "90"), samples);
  assert.deepEqual(selectHistorySamples(samples, "all"), samples);
});

test("history periods are anchored to the publication, including a stale or single sample", () => {
  const samples = [sample("2020-01-01", 0)];
  assert.deepEqual(selectHistorySamples(samples, "7"), samples);
  assert.throws(() => selectHistorySamples([], "all"), DashboardSchemaError);
});

test("net changes keep decreases and zero distinct from positive growth", () => {
  assert.equal(formatChange(1000), "+1,000");
  assert.equal(formatChange(-1000), "-1,000");
  assert.equal(formatChange(0), "0");
});
