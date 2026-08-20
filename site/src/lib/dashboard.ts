import type { ZodMiniType } from "zod/mini";

import {
  createDashboardHistorySchema,
  DASHBOARD_METRICS,
  DASHBOARD_SCHEMA_VERSION,
  dashboardDocumentSchema,
  isoDateSchema,
  type DashboardDistributionItem,
  type DashboardDocument,
  type DashboardHistoryDocument,
  type DashboardHistorySample,
  type DashboardMetric,
  type DashboardMetricName,
  type FreshnessName,
} from "./dashboard-schema.ts";

export {
  DASHBOARD_METRICS,
  DASHBOARD_SCHEMA_VERSION,
  type DashboardDistributionItem,
  type DashboardDocument,
  type DashboardHistoryDocument,
  type DashboardHistorySample,
  type DashboardMetric,
  type DashboardMetricName,
};

export const DASHBOARD_MAX_BYTES = 256 * 1024;
export const DASHBOARD_HISTORY_MAX_BYTES = 1_000_000;
export const DASHBOARD_FETCH_TIMEOUT_MS = 10_000;

const CURRENT_AGE_DAYS = 1;
const MILLISECONDS_PER_DAY = 86_400_000;

export const FRESHNESS_LABELS: Record<FreshnessName, string> = {
  today: "Updated today",
  days_1_7: "Updated 1-7 days ago",
  days_8_30: "Updated 8-30 days ago",
  days_31_plus: "Updated 31+ days ago",
  unknown: "Update date unknown",
};

export type PublicationState = "current" | "future" | "stale";

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
  return parseSchema(dashboardDocumentSchema, value, "dashboard");
}

export function parseDashboardHistory(
  value: unknown,
  dashboard: DashboardDocument,
): DashboardHistoryDocument {
  return parseSchema(
    createDashboardHistorySchema(dashboard),
    value,
    "dashboard history",
  );
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
        throw new DashboardLoadError(
          "invalid",
          `${label} response is too large`,
        );
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

function decodeUtf8(content: Uint8Array, label: string): string {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(content);
  } catch {
    throw new DashboardLoadError("invalid", `${label} response is not UTF-8`);
  }
}

function isoDate(value: unknown, label: string): string {
  return parseSchema(isoDateSchema, value, label);
}

function parseSchema<T>(
  schema: ZodMiniType<T>,
  value: unknown,
  label: string,
): T {
  const result = schema.safeParse(value);
  if (result.success) {
    return result.data;
  }
  const issue = result.error.issues[0];
  if (issue === undefined) {
    throw new DashboardSchemaError(`${label} is invalid`);
  }
  const path = [label, ...issue.path.map(String)].join(".");
  throw new DashboardSchemaError(`${path}: ${issue.message}`);
}
