import {
  DashboardSchemaError,
  formatCount,
  MILLISECONDS_PER_DAY,
  type DashboardHistorySample,
} from "./dashboard.ts";

export const HISTORY_METRICS = {
  packages: "Packages",
  owners: "Owners",
  repositories: "Repositories",
} as const;

export type HistoryMetric = keyof typeof HISTORY_METRICS;
export type HistoryPeriod = "7" | "30" | "90" | "all";

export function selectHistorySamples(
  samples: ReadonlyArray<DashboardHistorySample>,
  period: HistoryPeriod,
): ReadonlyArray<DashboardHistorySample> {
  const latest = samples.at(-1);
  if (latest === undefined) {
    throw new DashboardSchemaError("dashboard history has no samples");
  }
  if (period === "all") {
    return samples;
  }
  const earliest =
    Date.parse(`${latest.date}T00:00:00.000Z`) -
    (Number(period) - 1) * MILLISECONDS_PER_DAY;
  return samples.filter(
    (sample) => Date.parse(`${sample.date}T00:00:00.000Z`) >= earliest,
  );
}

export function formatChange(value: number): string {
  return `${value > 0 ? "+" : ""}${formatCount(value)}`;
}
