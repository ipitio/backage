export const DASHBOARD_SCHEMA_VERSION = 1;
export const DASHBOARD_MAX_BYTES = 256 * 1024;
export const DASHBOARD_HISTORY_MAX_BYTES = 1_000_000;
export const DASHBOARD_FETCH_TIMEOUT_MS = 10_000;

const CURRENT_AGE_DAYS = 1;
const DASHBOARD_HISTORY_RETENTION_DAYS = 180;
const DASHBOARD_HISTORY_SCHEMA_VERSION = 1;
const DASHBOARD_PACKAGE_TYPE_LIMIT = 16;
const MILLISECONDS_PER_DAY = 86_400_000;
const FRESHNESS_NAMES = [
  "today",
  "days_1_7",
  "days_8_30",
  "days_31_plus",
  "unknown",
] as const;

export const DASHBOARD_METRICS = [
  { label: "Downloadable size", name: "size", unit: "bytes" },
  { label: "Daily downloads", name: "downloads_day", unit: "downloads" },
  { label: "Weekly downloads", name: "downloads_week", unit: "downloads" },
  { label: "Monthly downloads", name: "downloads_month", unit: "downloads" },
  { label: "Total downloads", name: "downloads_total", unit: "downloads" },
] as const;

export const FRESHNESS_LABELS: Record<FreshnessName, string> = {
  today: "Updated today",
  days_1_7: "Updated 1-7 days ago",
  days_8_30: "Updated 8-30 days ago",
  days_31_plus: "Updated 31+ days ago",
  unknown: "Update date unknown",
};

type FreshnessName = (typeof FRESHNESS_NAMES)[number];
export type DashboardMetricName = (typeof DASHBOARD_METRICS)[number]["name"];
export type PublicationState = "current" | "future" | "stale";

export interface DashboardDistributionItem {
  coverage_basis_points: number;
  name: string;
  packages: number;
}

export interface DashboardMetric {
  coverage_basis_points: number;
  denominator: "catalog_packages";
  known_packages: number;
  unit: "bytes" | "downloads";
  unknown_packages: number;
  unknown_treatment: "negative_or_missing_current_value";
  value: number;
}

export interface DashboardDocument {
  freshness: {
    buckets: ReadonlyArray<DashboardDistributionItem & { name: FreshnessName }>;
    denominator: "catalog_packages";
    unit: "packages";
    unknown_treatment: "missing_invalid_or_future_observed_date";
  };
  generated_date: string;
  history: {
    path: "dashboard-history.json";
    retention_days: number;
    schema_version: number;
  };
  inventory: {
    owners: number;
    packages: number;
    repositories: number;
    resolved_packages: number;
  };
  metrics: Record<DashboardMetricName, DashboardMetric>;
  package_types: {
    denominator: "catalog_packages";
    items: ReadonlyArray<DashboardDistributionItem>;
    limit: number;
    other_coverage_basis_points: number;
    other_packages: number;
    unit: "packages";
  };
  schema_version: typeof DASHBOARD_SCHEMA_VERSION;
}

export interface DashboardHistorySample {
  date: string;
  downloads_known_packages: number;
  owners: number;
  packages: number;
  repositories: number;
  size_known_packages: number;
}

export interface DashboardHistoryDocument {
  retention_days: typeof DASHBOARD_HISTORY_RETENTION_DAYS;
  samples: ReadonlyArray<DashboardHistorySample>;
  schema_version: typeof DASHBOARD_HISTORY_SCHEMA_VERSION;
}

export class DashboardSchemaError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DashboardSchemaError";
  }
}

export class DashboardLoadError extends Error {
  readonly kind: "invalid" | "network";

  constructor(kind: "invalid" | "network", message: string) {
    super(message);
    this.name = "DashboardLoadError";
    this.kind = kind;
  }
}

export interface DashboardLoadOptions {
  fetcher?: typeof fetch;
  maxBytes?: number;
  signal?: AbortSignal;
}

export async function loadDashboard(
  url: string,
  options: DashboardLoadOptions = {},
): Promise<DashboardDocument> {
  return loadJsonDocument(
    url,
    "dashboard",
    options.maxBytes ?? DASHBOARD_MAX_BYTES,
    options,
    parseDashboard,
  );
}

export async function loadDashboardHistory(
  url: string,
  dashboard: DashboardDocument,
  options: DashboardLoadOptions = {},
): Promise<DashboardHistoryDocument> {
  return loadJsonDocument(
    url,
    "dashboard history",
    options.maxBytes ?? DASHBOARD_HISTORY_MAX_BYTES,
    options,
    (value) => parseDashboardHistory(value, dashboard),
  );
}

async function loadJsonDocument<T>(
  url: string,
  label: string,
  maxBytes: number,
  options: DashboardLoadOptions,
  parse: (value: unknown) => T,
): Promise<T> {
  if (!Number.isSafeInteger(maxBytes) || maxBytes <= 0) {
    throw new RangeError(`${label} byte limit must be a positive safe integer`);
  }

  let response: Response;
  const request: RequestInit = {
    cache: "no-cache",
    headers: { Accept: "application/json" },
  };
  if (options.signal !== undefined) {
    request.signal = options.signal;
  }
  try {
    response = await (options.fetcher ?? globalThis.fetch)(url, request);
  } catch (error) {
    const detail = error instanceof Error ? error.message : "request failed";
    throw new DashboardLoadError("network", detail);
  }
  if (!response.ok) {
    throw new DashboardLoadError(
      "network",
      `${label} request returned HTTP ${response.status}`,
    );
  }

  const declaredLength = response.headers.get("content-length");
  if (declaredLength !== null) {
    const bytes = Number(declaredLength);
    if (Number.isFinite(bytes) && bytes > maxBytes) {
      throw new DashboardLoadError("invalid", `${label} response is too large`);
    }
  }

  const content = await readBoundedBody(response, maxBytes, label);
  let value: unknown;
  try {
    value = JSON.parse(content) as unknown;
  } catch {
    throw new DashboardLoadError(
      "invalid",
      `${label} response is not valid JSON`,
    );
  }
  return parse(value);
}

export function parseDashboard(value: unknown): DashboardDocument {
  const document = exactObject(value, "dashboard", [
    "schema_version",
    "generated_date",
    "inventory",
    "package_types",
    "freshness",
    "metrics",
    "history",
  ]);
  const schemaVersion = safeInteger(document.schema_version, "schema_version");
  if (schemaVersion !== DASHBOARD_SCHEMA_VERSION) {
    throw new DashboardSchemaError(
      `unsupported dashboard schema version ${schemaVersion}`,
    );
  }
  const generatedDate = isoDate(document.generated_date, "generated_date");
  const inventory = parseInventory(document.inventory);
  const packageTypes = parsePackageTypes(
    document.package_types,
    inventory.packages,
  );
  const freshness = parseFreshness(document.freshness, inventory.packages);
  const metrics = parseMetrics(document.metrics, inventory.packages);
  const history = parseHistory(document.history);

  return {
    freshness,
    generated_date: generatedDate,
    history,
    inventory,
    metrics,
    package_types: packageTypes,
    schema_version: DASHBOARD_SCHEMA_VERSION,
  };
}

export function parseDashboardHistory(
  value: unknown,
  dashboard: DashboardDocument,
): DashboardHistoryDocument {
  const expectedDate = dashboard.generated_date;
  const document = exactObject(value, "dashboard history", [
    "schema_version",
    "retention_days",
    "samples",
  ]);
  const schemaVersion = positiveInteger(
    document.schema_version,
    "dashboard history.schema_version",
  );
  if (schemaVersion !== DASHBOARD_HISTORY_SCHEMA_VERSION) {
    throw new DashboardSchemaError(
      `dashboard history.schema_version must be ${DASHBOARD_HISTORY_SCHEMA_VERSION}`,
    );
  }
  const retentionDays = positiveInteger(
    document.retention_days,
    "dashboard history.retention_days",
  );
  if (retentionDays !== DASHBOARD_HISTORY_RETENTION_DAYS) {
    throw new DashboardSchemaError(
      `dashboard history.retention_days must be ${DASHBOARD_HISTORY_RETENTION_DAYS}`,
    );
  }
  if (
    !Array.isArray(document.samples) ||
    document.samples.length === 0 ||
    document.samples.length > DASHBOARD_HISTORY_RETENTION_DAYS
  ) {
    throw new DashboardSchemaError("dashboard history.samples has an invalid length");
  }
  const samples = document.samples.map((sample, index) =>
    parseHistorySample(sample, index),
  );
  for (let index = 1; index < samples.length; index += 1) {
    const previous = samples[index - 1];
    const current = samples[index];
    if (previous === undefined || current === undefined) {
      throw new DashboardSchemaError("dashboard history sample is unavailable");
    }
    if (current.date <= previous.date) {
      throw new DashboardSchemaError(
        "dashboard history.samples must use increasing unique dates",
      );
    }
  }
  if (samples.at(-1)?.date !== expectedDate) {
    throw new DashboardSchemaError(
      "dashboard history does not match the current publication date",
    );
  }
  const latest = samples.at(-1);
  if (
    latest === undefined ||
    latest.owners !== dashboard.inventory.owners ||
    latest.repositories !== dashboard.inventory.repositories ||
    latest.packages !== dashboard.inventory.packages ||
    latest.size_known_packages !== dashboard.metrics.size.known_packages ||
    latest.downloads_known_packages !==
      dashboard.metrics.downloads_total.known_packages
  ) {
    throw new DashboardSchemaError(
      "dashboard history does not match the current projection",
    );
  }
  const earliestAllowed = new Date(
    Date.parse(`${expectedDate}T00:00:00.000Z`) -
      (DASHBOARD_HISTORY_RETENTION_DAYS - 1) * MILLISECONDS_PER_DAY,
  )
    .toISOString()
    .slice(0, 10);
  if ((samples[0]?.date ?? expectedDate) < earliestAllowed) {
    throw new DashboardSchemaError(
      "dashboard history exceeds its retention window",
    );
  }
  return {
    retention_days: DASHBOARD_HISTORY_RETENTION_DAYS,
    samples,
    schema_version: DASHBOARD_HISTORY_SCHEMA_VERSION,
  };
}

export function publicationState(
  generatedDate: string,
  now: Date = new Date(),
): PublicationState {
  const generated = isoDate(generatedDate, "generated_date");
  if (Number.isNaN(now.getTime())) {
    throw new RangeError("current date must be valid");
  }
  const generatedTime = Date.parse(`${generated}T00:00:00.000Z`);
  const currentTime = Date.UTC(
    now.getUTCFullYear(),
    now.getUTCMonth(),
    now.getUTCDate(),
  );
  const ageDays = Math.floor(
    (currentTime - generatedTime) / MILLISECONDS_PER_DAY,
  );
  if (ageDays < 0) {
    return "future";
  }
  return ageDays > CURRENT_AGE_DAYS ? "stale" : "current";
}

export function formatCount(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

export function formatCoverage(basisPoints: number): string {
  return new Intl.NumberFormat("en-US", {
    maximumFractionDigits: 2,
    style: "percent",
  }).format(basisPoints / 10_000);
}

export function formatBytes(value: number): string {
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"] as const;
  let amount = value;
  let unitIndex = 0;
  while (amount >= 1024 && unitIndex < units.length - 1) {
    amount /= 1024;
    unitIndex += 1;
  }
  const unit = units[unitIndex];
  if (unit === undefined) {
    throw new RangeError("byte unit is unavailable");
  }
  const formatted = new Intl.NumberFormat("en-US", {
    maximumFractionDigits: unitIndex === 0 ? 0 : 1,
  }).format(amount);
  return `${formatted} ${unit}`;
}

export function formatPublicationDate(value: string): string {
  const date = isoDate(value, "generated_date");
  return new Intl.DateTimeFormat("en-US", {
    day: "numeric",
    month: "long",
    timeZone: "UTC",
    year: "numeric",
  }).format(new Date(`${date}T00:00:00.000Z`));
}

async function readBoundedBody(
  response: Response,
  maxBytes: number,
  label: string,
): Promise<string> {
  if (response.body === null) {
    const content = new Uint8Array(await response.arrayBuffer());
    if (content.byteLength > maxBytes) {
      throw new DashboardLoadError("invalid", `${label} response is too large`);
    }
    return decodeUtf8(content, label);
  }

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const result = await reader.read();
      if (result.done) {
        break;
      }
      total += result.value.byteLength;
      if (total > maxBytes) {
        await reader.cancel();
        throw new DashboardLoadError("invalid", `${label} response is too large`);
      }
      chunks.push(result.value);
    }
  } finally {
    reader.releaseLock();
  }

  const content = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    content.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return decodeUtf8(content, label);
}

function parseHistorySample(
  value: unknown,
  index: number,
): DashboardHistorySample {
  const label = `dashboard history.samples[${index}]`;
  const sample = exactObject(value, label, [
    "date",
    "owners",
    "repositories",
    "packages",
    "size_known_packages",
    "downloads_known_packages",
  ]);
  const packages = count(sample.packages, `${label}.packages`);
  const owners = count(sample.owners, `${label}.owners`);
  const repositories = count(sample.repositories, `${label}.repositories`);
  const sizeKnownPackages = count(
    sample.size_known_packages,
    `${label}.size_known_packages`,
  );
  const downloadsKnownPackages = count(
    sample.downloads_known_packages,
    `${label}.downloads_known_packages`,
  );
  if (
    owners > repositories ||
    repositories > packages ||
    sizeKnownPackages > packages ||
    downloadsKnownPackages > packages
  ) {
    throw new DashboardSchemaError(`${label} has inconsistent catalog counts`);
  }
  return {
    date: isoDate(sample.date, `${label}.date`),
    downloads_known_packages: downloadsKnownPackages,
    owners,
    packages,
    repositories,
    size_known_packages: sizeKnownPackages,
  };
}

function decodeUtf8(content: Uint8Array, label: string): string {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(content);
  } catch {
    throw new DashboardLoadError("invalid", `${label} response is not UTF-8`);
  }
}

function parseInventory(value: unknown): DashboardDocument["inventory"] {
  const inventory = exactObject(value, "inventory", [
    "owners",
    "repositories",
    "packages",
    "resolved_packages",
  ]);
  const packages = count(inventory.packages, "inventory.packages");
  const resolvedPackages = count(
    inventory.resolved_packages,
    "inventory.resolved_packages",
  );
  const owners = count(inventory.owners, "inventory.owners");
  const repositories = count(
    inventory.repositories,
    "inventory.repositories",
  );
  if (owners > repositories || repositories > packages) {
    throw new DashboardSchemaError("inventory has inconsistent catalog counts");
  }
  if (resolvedPackages > packages) {
    throw new DashboardSchemaError(
      "inventory.resolved_packages exceeds inventory.packages",
    );
  }
  return {
    owners,
    packages,
    repositories,
    resolved_packages: resolvedPackages,
  };
}

function parsePackageTypes(
  value: unknown,
  totalPackages: number,
): DashboardDocument["package_types"] {
  const packageTypes = exactObject(value, "package_types", [
    "unit",
    "denominator",
    "limit",
    "items",
    "other_packages",
    "other_coverage_basis_points",
  ]);
  literal(packageTypes.unit, "packages", "package_types.unit");
  literal(
    packageTypes.denominator,
    "catalog_packages",
    "package_types.denominator",
  );
  const limit = positiveInteger(packageTypes.limit, "package_types.limit");
  if (limit !== DASHBOARD_PACKAGE_TYPE_LIMIT) {
    throw new DashboardSchemaError(
      `package_types.limit must be ${DASHBOARD_PACKAGE_TYPE_LIMIT}`,
    );
  }
  if (!Array.isArray(packageTypes.items) || packageTypes.items.length > limit) {
    throw new DashboardSchemaError("package_types.items exceeds its limit");
  }
  const items = packageTypes.items.map((item, index) =>
    parseDistributionItem(item, `package_types.items[${index}]`, totalPackages),
  );
  ensureUniqueNames(items, "package_types.items");
  const otherPackages = count(
    packageTypes.other_packages,
    "package_types.other_packages",
  );
  const otherCoverage = basisPoints(
    packageTypes.other_coverage_basis_points,
    "package_types.other_coverage_basis_points",
  );
  verifyCoverage(otherPackages, totalPackages, otherCoverage, "package_types.other");
  verifyPartition(
    [...items.map((item) => item.packages), otherPackages],
    totalPackages,
    "package_types",
  );
  return {
    denominator: "catalog_packages",
    items,
    limit,
    other_coverage_basis_points: otherCoverage,
    other_packages: otherPackages,
    unit: "packages",
  };
}

function parseFreshness(
  value: unknown,
  totalPackages: number,
): DashboardDocument["freshness"] {
  const freshness = exactObject(value, "freshness", [
    "unit",
    "denominator",
    "unknown_treatment",
    "buckets",
  ]);
  literal(freshness.unit, "packages", "freshness.unit");
  literal(
    freshness.denominator,
    "catalog_packages",
    "freshness.denominator",
  );
  literal(
    freshness.unknown_treatment,
    "missing_invalid_or_future_observed_date",
    "freshness.unknown_treatment",
  );
  if (
    !Array.isArray(freshness.buckets) ||
    freshness.buckets.length !== FRESHNESS_NAMES.length
  ) {
    throw new DashboardSchemaError("freshness.buckets has an invalid length");
  }
  const buckets = freshness.buckets.map((item, index) => {
    const parsed = parseDistributionItem(
      item,
      `freshness.buckets[${index}]`,
      totalPackages,
    );
    const expectedName = FRESHNESS_NAMES[index];
    if (expectedName === undefined) {
      throw new DashboardSchemaError(`freshness.buckets[${index}] is unexpected`);
    }
    if (parsed.name !== expectedName) {
      throw new DashboardSchemaError(
        `freshness.buckets[${index}].name must be ${expectedName}`,
      );
    }
    return { ...parsed, name: expectedName };
  });
  verifyPartition(
    buckets.map((bucket) => bucket.packages),
    totalPackages,
    "freshness",
  );
  return {
    buckets,
    denominator: "catalog_packages",
    unit: "packages",
    unknown_treatment: "missing_invalid_or_future_observed_date",
  };
}

function parseMetrics(
  value: unknown,
  totalPackages: number,
): DashboardDocument["metrics"] {
  const metrics = exactObject(
    value,
    "metrics",
    DASHBOARD_METRICS.map((metric) => metric.name),
  );
  return {
    size: parseMetric(metrics.size, "size", "bytes", totalPackages),
    downloads_day: parseMetric(
      metrics.downloads_day,
      "downloads_day",
      "downloads",
      totalPackages,
    ),
    downloads_week: parseMetric(
      metrics.downloads_week,
      "downloads_week",
      "downloads",
      totalPackages,
    ),
    downloads_month: parseMetric(
      metrics.downloads_month,
      "downloads_month",
      "downloads",
      totalPackages,
    ),
    downloads_total: parseMetric(
      metrics.downloads_total,
      "downloads_total",
      "downloads",
      totalPackages,
    ),
  };
}

function parseMetric(
  value: unknown,
  name: DashboardMetricName,
  unit: DashboardMetric["unit"],
  totalPackages: number,
): DashboardMetric {
  const metric = exactObject(value, `metrics.${name}`, [
    "unit",
    "denominator",
    "unknown_treatment",
    "known_packages",
    "unknown_packages",
    "coverage_basis_points",
    "value",
  ]);
  literal(metric.unit, unit, `metrics.${name}.unit`);
  literal(
    metric.denominator,
    "catalog_packages",
    `metrics.${name}.denominator`,
  );
  literal(
    metric.unknown_treatment,
    "negative_or_missing_current_value",
    `metrics.${name}.unknown_treatment`,
  );
  const knownPackages = count(
    metric.known_packages,
    `metrics.${name}.known_packages`,
  );
  const unknownPackages = count(
    metric.unknown_packages,
    `metrics.${name}.unknown_packages`,
  );
  verifyPartition(
    [knownPackages, unknownPackages],
    totalPackages,
    `metrics.${name}`,
  );
  const coverage = basisPoints(
    metric.coverage_basis_points,
    `metrics.${name}.coverage_basis_points`,
  );
  verifyCoverage(knownPackages, totalPackages, coverage, `metrics.${name}`);
  return {
    coverage_basis_points: coverage,
    denominator: "catalog_packages",
    known_packages: knownPackages,
    unit,
    unknown_packages: unknownPackages,
    unknown_treatment: "negative_or_missing_current_value",
    value: count(metric.value, `metrics.${name}.value`),
  };
}

function parseHistory(value: unknown): DashboardDocument["history"] {
  const history = exactObject(value, "history", [
    "path",
    "schema_version",
    "retention_days",
  ]);
  literal(history.path, "dashboard-history.json", "history.path");
  const retentionDays = positiveInteger(
    history.retention_days,
    "history.retention_days",
  );
  if (retentionDays !== DASHBOARD_HISTORY_RETENTION_DAYS) {
    throw new DashboardSchemaError(
      `history.retention_days must be ${DASHBOARD_HISTORY_RETENTION_DAYS}`,
    );
  }
  const schemaVersion = positiveInteger(
    history.schema_version,
    "history.schema_version",
  );
  if (schemaVersion !== DASHBOARD_HISTORY_SCHEMA_VERSION) {
    throw new DashboardSchemaError(
      `history.schema_version must be ${DASHBOARD_HISTORY_SCHEMA_VERSION}`,
    );
  }
  return {
    path: "dashboard-history.json",
    retention_days: DASHBOARD_HISTORY_RETENTION_DAYS,
    schema_version: DASHBOARD_HISTORY_SCHEMA_VERSION,
  };
}

function parseDistributionItem(
  value: unknown,
  label: string,
  totalPackages: number,
): DashboardDistributionItem {
  const item = exactObject(value, label, [
    "name",
    "packages",
    "coverage_basis_points",
  ]);
  const name = nonemptyString(item.name, `${label}.name`);
  const packages = count(item.packages, `${label}.packages`);
  const coverage = basisPoints(
    item.coverage_basis_points,
    `${label}.coverage_basis_points`,
  );
  verifyCoverage(packages, totalPackages, coverage, label);
  return { coverage_basis_points: coverage, name, packages };
}

function exactObject(
  value: unknown,
  label: string,
  fields: ReadonlyArray<string>,
): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new DashboardSchemaError(`${label} must be an object`);
  }
  const document = value as Record<string, unknown>;
  const actual = Object.keys(document);
  if (
    actual.length !== fields.length ||
    !fields.every((field) => Object.hasOwn(document, field))
  ) {
    throw new DashboardSchemaError(`${label} has unexpected fields`);
  }
  return document;
}

function safeInteger(value: unknown, label: string): number {
  if (!Number.isSafeInteger(value)) {
    throw new DashboardSchemaError(`${label} must be a safe integer`);
  }
  return value as number;
}

function count(value: unknown, label: string): number {
  const parsed = safeInteger(value, label);
  if (parsed < 0) {
    throw new DashboardSchemaError(`${label} must not be negative`);
  }
  return parsed;
}

function positiveInteger(value: unknown, label: string): number {
  const parsed = safeInteger(value, label);
  if (parsed <= 0) {
    throw new DashboardSchemaError(`${label} must be positive`);
  }
  return parsed;
}

function basisPoints(value: unknown, label: string): number {
  const parsed = count(value, label);
  if (parsed > 10_000) {
    throw new DashboardSchemaError(`${label} exceeds 100 percent`);
  }
  return parsed;
}

function nonemptyString(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new DashboardSchemaError(`${label} must be a nonempty string`);
  }
  return value;
}

function literal<T extends string>(
  value: unknown,
  expected: T,
  label: string,
): T {
  if (value !== expected) {
    throw new DashboardSchemaError(`${label} must be ${expected}`);
  }
  return expected;
}

function isoDate(value: unknown, label: string): string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    throw new DashboardSchemaError(`${label} must use YYYY-MM-DD`);
  }
  const parsed = new Date(`${value}T00:00:00.000Z`);
  if (
    Number.isNaN(parsed.getTime()) ||
    parsed.toISOString().slice(0, 10) !== value
  ) {
    throw new DashboardSchemaError(`${label} is not a valid date`);
  }
  return value;
}

function verifyCoverage(
  packages: number,
  totalPackages: number,
  actual: number,
  label: string,
): void {
  const expected =
    totalPackages === 0
      ? 0
      : Number((BigInt(packages) * 10_000n) / BigInt(totalPackages));
  if (actual !== expected) {
    throw new DashboardSchemaError(`${label} coverage does not match its count`);
  }
}

function verifyPartition(
  values: ReadonlyArray<number>,
  total: number,
  label: string,
): void {
  const sum = values.reduce((current, value) => current + BigInt(value), 0n);
  if (sum !== BigInt(total)) {
    throw new DashboardSchemaError(`${label} does not partition the catalog`);
  }
}

function ensureUniqueNames(
  items: ReadonlyArray<DashboardDistributionItem>,
  label: string,
): void {
  if (new Set(items.map((item) => item.name)).size !== items.length) {
    throw new DashboardSchemaError(`${label} contains duplicate names`);
  }
}
