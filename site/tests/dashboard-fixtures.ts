const MILLISECONDS_PER_DAY = 86_400_000;

export function utcDate(offsetDays = 0): string {
  const now = new Date();
  const today = Date.UTC(
    now.getUTCFullYear(),
    now.getUTCMonth(),
    now.getUTCDate(),
  );
  return new Date(today + offsetDays * MILLISECONDS_PER_DAY)
    .toISOString()
    .slice(0, 10);
}

export function dashboardFixture(generatedDate = utcDate()): object {
  return {
    schema_version: 1,
    generated_date: generatedDate,
    inventory: {
      owners: 12,
      repositories: 345,
      packages: 1_200,
      resolved_packages: 1_175,
    },
    package_types: {
      unit: "packages",
      denominator: "catalog_packages",
      limit: 16,
      items: [
        { name: "container", packages: 700, coverage_basis_points: 5_833 },
        { name: "npm", packages: 300, coverage_basis_points: 2_500 },
        { name: "maven", packages: 150, coverage_basis_points: 1_250 },
      ],
      other_packages: 50,
      other_coverage_basis_points: 416,
    },
    freshness: {
      unit: "packages",
      denominator: "catalog_packages",
      unknown_treatment: "missing_invalid_or_future_observed_date",
      buckets: [
        { name: "today", packages: 500, coverage_basis_points: 4_166 },
        { name: "days_1_7", packages: 400, coverage_basis_points: 3_333 },
        { name: "days_8_30", packages: 200, coverage_basis_points: 1_666 },
        { name: "days_31_plus", packages: 50, coverage_basis_points: 416 },
        { name: "unknown", packages: 50, coverage_basis_points: 416 },
      ],
    },
    metrics: {
      size: metric("bytes", 1_000, 200, 8_333, 1_610_612_736),
      downloads_day: metric("downloads", 800, 400, 6_666, 12_000),
      downloads_week: metric("downloads", 850, 350, 7_083, 84_000),
      downloads_month: metric("downloads", 900, 300, 7_500, 360_000),
      downloads_total: metric("downloads", 1_100, 100, 9_166, 9_000_000),
    },
    history: {
      path: "dashboard-history.json",
      schema_version: 1,
      retention_days: 180,
    },
  };
}

export function historyFixture(generatedDate = utcDate()): object {
  return {
    schema_version: 1,
    retention_days: 180,
    samples: [
      historySample(utcDateFrom(generatedDate, -2), 11, 340, 1_180, 970, 1_070),
      historySample(utcDateFrom(generatedDate, -1), 12, 343, 1_190, 985, 1_085),
      historySample(generatedDate, 12, 345, 1_200, 1_000, 1_100),
    ],
  };
}

function utcDateFrom(value: string, offsetDays: number): string {
  return new Date(
    Date.parse(`${value}T00:00:00.000Z`) + offsetDays * MILLISECONDS_PER_DAY,
  )
    .toISOString()
    .slice(0, 10);
}

function historySample(
  date: string,
  owners: number,
  repositories: number,
  packages: number,
  sizeKnownPackages: number,
  downloadsKnownPackages: number,
): object {
  return {
    date,
    owners,
    repositories,
    packages,
    size_known_packages: sizeKnownPackages,
    downloads_known_packages: downloadsKnownPackages,
  };
}

function metric(
  unit: "bytes" | "downloads",
  knownPackages: number,
  unknownPackages: number,
  coverageBasisPoints: number,
  value: number,
): object {
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
