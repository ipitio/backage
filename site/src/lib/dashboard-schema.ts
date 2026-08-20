import { z } from "zod/mini";

type RefinementCtx = z.core.$RefinementCtx;

export const DASHBOARD_SCHEMA_VERSION = 1;
export const DASHBOARD_HISTORY_RETENTION_DAYS = 180;

const DASHBOARD_HISTORY_SCHEMA_VERSION = 1;
const DASHBOARD_PACKAGE_TYPE_LIMIT = 16;
const MILLISECONDS_PER_DAY = 86_400_000;

export const DASHBOARD_METRICS = [
  { label: "Downloadable size", name: "size", unit: "bytes" },
  { label: "Daily downloads", name: "downloads_day", unit: "downloads" },
  { label: "Weekly downloads", name: "downloads_week", unit: "downloads" },
  { label: "Monthly downloads", name: "downloads_month", unit: "downloads" },
  { label: "Total downloads", name: "downloads_total", unit: "downloads" },
] as const;

const safeIntegerSchema = z.int();
const countSchema = z.int().check(z.nonnegative());
const basisPointsSchema = countSchema.check(z.maximum(10_000));
export const isoDateSchema = z.iso.date();

const dashboardSchemaVersionSchema = z.pipe(
  safeIntegerSchema.check(
    z.superRefine((value, context) => {
      if (value !== DASHBOARD_SCHEMA_VERSION) {
        addIssue(context, [], `unsupported dashboard schema version ${value}`);
      }
    }),
  ),
  z.transform(() => DASHBOARD_SCHEMA_VERSION),
);

const distributionItemSchema = z.strictObject({
  coverage_basis_points: basisPointsSchema,
  name: z.string().check(z.minLength(1)),
  packages: countSchema,
});

function createMetricSchema<const T extends "bytes" | "downloads">(unit: T) {
  return z.strictObject({
    coverage_basis_points: basisPointsSchema,
    denominator: z.literal("catalog_packages"),
    known_packages: countSchema,
    unit: z.literal(unit),
    unknown_packages: countSchema,
    unknown_treatment: z.literal("negative_or_missing_current_value"),
    value: countSchema,
  });
}

const inventorySchema = z.strictObject({
  owners: countSchema,
  packages: countSchema,
  repositories: countSchema,
  resolved_packages: countSchema,
});

const packageTypesSchema = z.strictObject({
  denominator: z.literal("catalog_packages"),
  items: z
    .array(distributionItemSchema)
    .check(z.maxLength(DASHBOARD_PACKAGE_TYPE_LIMIT)),
  limit: z.literal(DASHBOARD_PACKAGE_TYPE_LIMIT),
  other_coverage_basis_points: basisPointsSchema,
  other_packages: countSchema,
  unit: z.literal("packages"),
});

const freshnessSchema = z.strictObject({
  buckets: z.tuple([
    z.extend(distributionItemSchema, { name: z.literal("today") }),
    z.extend(distributionItemSchema, { name: z.literal("days_1_7") }),
    z.extend(distributionItemSchema, { name: z.literal("days_8_30") }),
    z.extend(distributionItemSchema, { name: z.literal("days_31_plus") }),
    z.extend(distributionItemSchema, { name: z.literal("unknown") }),
  ]),
  denominator: z.literal("catalog_packages"),
  unit: z.literal("packages"),
  unknown_treatment: z.literal("missing_invalid_or_future_observed_date"),
});

const metricsSchema = z.strictObject({
  downloads_day: createMetricSchema("downloads"),
  downloads_month: createMetricSchema("downloads"),
  downloads_total: createMetricSchema("downloads"),
  downloads_week: createMetricSchema("downloads"),
  size: createMetricSchema("bytes"),
});

const historyReferenceSchema = z.strictObject({
  path: z.literal("dashboard-history.json"),
  retention_days: z.literal(DASHBOARD_HISTORY_RETENTION_DAYS),
  schema_version: z.literal(DASHBOARD_HISTORY_SCHEMA_VERSION),
});

export const dashboardDocumentSchema = z
  .strictObject({
    freshness: freshnessSchema,
    generated_date: isoDateSchema,
    history: historyReferenceSchema,
    inventory: inventorySchema,
    metrics: metricsSchema,
    package_types: packageTypesSchema,
    schema_version: dashboardSchemaVersionSchema,
  })
  .check(
    z.superRefine((document, context) => {
      const { inventory } = document;
      if (
        inventory.owners > inventory.repositories ||
        inventory.repositories > inventory.packages
      ) {
        addIssue(
          context,
          ["inventory"],
          "inventory has inconsistent catalog counts",
        );
      }
      if (inventory.resolved_packages > inventory.packages) {
        addIssue(
          context,
          ["inventory", "resolved_packages"],
          "inventory.resolved_packages exceeds inventory.packages",
        );
      }

      const totalPackages = inventory.packages;
      const packageTypes = document.package_types;
      packageTypes.items.forEach((item, index) => {
        verifyCoverage(
          context,
          item.packages,
          totalPackages,
          item.coverage_basis_points,
          ["package_types", "items", index],
          `package_types.items[${index}]`,
        );
      });
      verifyUniqueNames(context, packageTypes.items, [
        "package_types",
        "items",
      ]);
      verifyCoverage(
        context,
        packageTypes.other_packages,
        totalPackages,
        packageTypes.other_coverage_basis_points,
        ["package_types", "other_coverage_basis_points"],
        "package_types.other",
      );
      verifyPartition(
        context,
        [
          ...packageTypes.items.map((item) => item.packages),
          packageTypes.other_packages,
        ],
        totalPackages,
        ["package_types"],
        "package_types",
      );

      document.freshness.buckets.forEach((bucket, index) => {
        verifyCoverage(
          context,
          bucket.packages,
          totalPackages,
          bucket.coverage_basis_points,
          ["freshness", "buckets", index],
          `freshness.buckets[${index}]`,
        );
      });
      verifyPartition(
        context,
        document.freshness.buckets.map((bucket) => bucket.packages),
        totalPackages,
        ["freshness"],
        "freshness",
      );

      for (const metricDefinition of DASHBOARD_METRICS) {
        const metric = document.metrics[metricDefinition.name];
        const label = `metrics.${metricDefinition.name}`;
        verifyPartition(
          context,
          [metric.known_packages, metric.unknown_packages],
          totalPackages,
          ["metrics", metricDefinition.name],
          label,
        );
        verifyCoverage(
          context,
          metric.known_packages,
          totalPackages,
          metric.coverage_basis_points,
          ["metrics", metricDefinition.name, "coverage_basis_points"],
          label,
        );
      }
    }),
  );

const dashboardHistorySampleSchema = z.strictObject({
  date: isoDateSchema,
  downloads_known_packages: countSchema,
  owners: countSchema,
  packages: countSchema,
  repositories: countSchema,
  size_known_packages: countSchema,
});

const dashboardHistoryDocumentSchema = z.strictObject({
  retention_days: z.literal(DASHBOARD_HISTORY_RETENTION_DAYS),
  samples: z
    .array(dashboardHistorySampleSchema)
    .check(z.minLength(1), z.maxLength(DASHBOARD_HISTORY_RETENTION_DAYS)),
  schema_version: z.literal(DASHBOARD_HISTORY_SCHEMA_VERSION),
});

export type DashboardDocument = z.output<typeof dashboardDocumentSchema>;
export type DashboardDistributionItem = z.output<typeof distributionItemSchema>;
export type DashboardMetricName = (typeof DASHBOARD_METRICS)[number]["name"];
export type DashboardMetric = DashboardDocument["metrics"][DashboardMetricName];
export type FreshnessName =
  DashboardDocument["freshness"]["buckets"][number]["name"];
export type DashboardHistorySample = z.output<
  typeof dashboardHistorySampleSchema
>;
export type DashboardHistoryDocument = z.output<
  typeof dashboardHistoryDocumentSchema
>;

export function createDashboardHistorySchema(dashboard: DashboardDocument) {
  return dashboardHistoryDocumentSchema.check(
    z.superRefine((document, context) => {
      document.samples.forEach((sample, index) => {
        if (
          sample.owners > sample.repositories ||
          sample.repositories > sample.packages ||
          sample.size_known_packages > sample.packages ||
          sample.downloads_known_packages > sample.packages
        ) {
          addIssue(
            context,
            ["samples", index],
            `dashboard history.samples[${index}] has inconsistent catalog counts`,
          );
        }
      });

      for (let index = 1; index < document.samples.length; index += 1) {
        const previous = document.samples[index - 1];
        const current = document.samples[index];
        if (
          previous !== undefined &&
          current !== undefined &&
          current.date <= previous.date
        ) {
          addIssue(
            context,
            ["samples", index, "date"],
            "dashboard history.samples must use increasing unique dates",
          );
        }
      }

      const latest = document.samples.at(-1);
      if (latest?.date !== dashboard.generated_date) {
        addIssue(
          context,
          ["samples"],
          "dashboard history does not match the current publication date",
        );
      }
      if (
        latest === undefined ||
        latest.owners !== dashboard.inventory.owners ||
        latest.repositories !== dashboard.inventory.repositories ||
        latest.packages !== dashboard.inventory.packages ||
        latest.size_known_packages !== dashboard.metrics.size.known_packages ||
        latest.downloads_known_packages !==
          dashboard.metrics.downloads_total.known_packages
      ) {
        addIssue(
          context,
          ["samples"],
          "dashboard history does not match the current projection",
        );
      }

      const earliestAllowed = new Date(
        Date.parse(`${dashboard.generated_date}T00:00:00.000Z`) -
          (DASHBOARD_HISTORY_RETENTION_DAYS - 1) * MILLISECONDS_PER_DAY,
      )
        .toISOString()
        .slice(0, 10);
      if (
        (document.samples[0]?.date ?? dashboard.generated_date) <
        earliestAllowed
      ) {
        addIssue(
          context,
          ["samples", 0, "date"],
          "dashboard history exceeds its retention window",
        );
      }
    }),
  );
}

function verifyCoverage(
  context: RefinementCtx,
  packages: number,
  totalPackages: number,
  actual: number,
  path: ReadonlyArray<string | number>,
  label: string,
): void {
  const expected =
    totalPackages === 0
      ? 0
      : Number((BigInt(packages) * 10_000n) / BigInt(totalPackages));
  if (actual !== expected) {
    addIssue(context, path, `${label} coverage does not match its count`);
  }
}

function verifyPartition(
  context: RefinementCtx,
  values: ReadonlyArray<number>,
  total: number,
  path: ReadonlyArray<string | number>,
  label: string,
): void {
  const sum = values.reduce((current, value) => current + BigInt(value), 0n);
  if (sum !== BigInt(total)) {
    addIssue(context, path, `${label} does not partition the catalog`);
  }
}

function verifyUniqueNames(
  context: RefinementCtx,
  items: ReadonlyArray<DashboardDistributionItem>,
  path: ReadonlyArray<string | number>,
): void {
  if (new Set(items.map((item) => item.name)).size !== items.length) {
    addIssue(context, path, "package_types.items contains duplicate names");
  }
}

function addIssue(
  context: RefinementCtx,
  path: ReadonlyArray<string | number>,
  message: string,
): void {
  context.addIssue({ code: "custom", message, path: [...path] });
}
