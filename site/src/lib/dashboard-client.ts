import {
  DASHBOARD_FETCH_TIMEOUT_MS,
  DASHBOARD_METRICS,
  FRESHNESS_LABELS,
  DashboardLoadError,
  DashboardSchemaError,
  formatBytes,
  formatCount,
  formatCoverage,
  formatPublicationDate,
  loadDashboard,
  loadDashboardHistory,
  publicationState,
  type DashboardDistributionItem,
  type DashboardDocument,
  type DashboardHistoryDocument,
  type DashboardHistorySample,
} from "./dashboard";

export function startDashboard(): void {
  const dashboardUrl = requiredDashboardUrl();

  const status = element<HTMLElement>("publication-status");
  const statusLive = element<HTMLElement>("status-live");
  const statusTitle = element<HTMLElement>("status-title");
  const statusDetail = element<HTMLElement>("status-detail");
  const retry = element<HTMLButtonElement>("retry");
  const content = element<HTMLElement>("dashboard-content");
  let requestNumber = 0;

  function setStatus(
    tone: "current" | "error" | "loading" | "warning",
    title: string,
    detail: string,
  ): void {
    status.dataset.tone = tone;
    statusTitle.textContent = title;
    statusDetail.textContent = detail;
  }

  function distributionRow(
    item: DashboardDistributionItem,
    label: string,
  ): HTMLTableRowElement {
    const row = document.createElement("tr");
    const heading = document.createElement("th");
    heading.scope = "row";
    heading.textContent = label;

    const packages = document.createElement("td");
    packages.className = "numeric";
    packages.textContent = formatCount(item.packages);

    const share = document.createElement("td");
    share.append(coverageMeasure(item.coverage_basis_points));
    row.append(heading, packages, share);
    return row;
  }

  function renderDistributions(dashboard: DashboardDocument): void {
    const packageTypes = dashboard.package_types.items.map((item) =>
      distributionRow(item, packageTypeLabel(item.name)),
    );
    if (dashboard.package_types.other_packages > 0) {
      packageTypes.push(
        distributionRow(
          {
            coverage_basis_points:
              dashboard.package_types.other_coverage_basis_points,
            name: "other",
            packages: dashboard.package_types.other_packages,
          },
          "Other types",
        ),
      );
    }
    element<HTMLTableSectionElement>("package-types").replaceChildren(
      ...packageTypes,
    );

    const freshness = dashboard.freshness.buckets.map((bucket) =>
      distributionRow(bucket, FRESHNESS_LABELS[bucket.name]),
    );
    element<HTMLTableSectionElement>("freshness").replaceChildren(...freshness);
  }

  function renderMetrics(dashboard: DashboardDocument): void {
    const rows = DASHBOARD_METRICS.map((definition) => {
      const metric = dashboard.metrics[definition.name];
      const row = document.createElement("tr");
      const heading = document.createElement("th");
      heading.scope = "row";
      heading.textContent = definition.label;

      const aggregate = document.createElement("td");
      aggregate.className = "numeric";
      aggregate.textContent =
        metric.unit === "bytes"
          ? formatBytes(metric.value)
          : formatCount(metric.value);

      const known = document.createElement("td");
      known.className = "numeric";
      known.textContent = `${formatCount(metric.known_packages)} of ${formatCount(
        dashboard.inventory.packages,
      )}`;

      const coverage = document.createElement("td");
      coverage.append(coverageMeasure(metric.coverage_basis_points));
      row.append(heading, aggregate, known, coverage);
      return row;
    });
    element<HTMLTableSectionElement>("metrics").replaceChildren(...rows);
  }

  function renderDashboard(
    dashboard: DashboardDocument,
    currentRequest: number,
  ): void {
    setText("inventory-packages", formatCount(dashboard.inventory.packages));
    setText("inventory-owners", formatCount(dashboard.inventory.owners));
    setText(
      "inventory-repositories",
      formatCount(dashboard.inventory.repositories),
    );
    setText(
      "inventory-resolved",
      formatCount(dashboard.inventory.resolved_packages),
    );
    const date = formatPublicationDate(dashboard.generated_date);
    setText("publication-date", `Updated ${date} UTC`);
    renderDistributions(dashboard);
    renderMetrics(dashboard);

    const state = publicationState(dashboard.generated_date);
    if (state === "current") {
      setStatus("current", "Index snapshot current", `Published ${date} UTC.`);
    } else if (state === "stale") {
      setStatus(
        "warning",
        "Index snapshot may be stale",
        `The latest published data is dated ${date} UTC.`,
      );
    } else {
      setStatus(
        "warning",
        "Publication date mismatch",
        `The published data is dated ${date} UTC.`,
      );
    }
    statusLive.ariaBusy = "false";
    retry.hidden = true;
    content.hidden = false;
    void renderHistory(dashboard, currentRequest);
  }

  async function renderHistory(
    dashboard: DashboardDocument,
    currentRequest: number,
  ): Promise<void> {
    const historyStatus = element<HTMLElement>("history-status");
    const historyContent = element<HTMLElement>("history-content");
    const unavailable = element<HTMLElement>("history-unavailable");
    historyStatus.textContent = "Loading daily history.";
    historyContent.hidden = true;
    unavailable.hidden = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(
      () => controller.abort(),
      DASHBOARD_FETCH_TIMEOUT_MS,
    );
    try {
      const dashboardAbsoluteUrl = new URL(dashboardUrl, document.baseURI);
      const history = await loadDashboardHistory(
        new URL(dashboard.history.path, dashboardAbsoluteUrl).toString(),
        dashboard,
        { signal: controller.signal },
      );
      if (currentRequest !== requestNumber) {
        return;
      }
      await renderHistoryDocument(history);
      historyStatus.textContent = historyPeriod(history.samples);
      historyContent.hidden = false;
    } catch (error) {
      if (currentRequest !== requestNumber) {
        return;
      }
      historyStatus.textContent = "History unavailable";
      unavailable.hidden = false;
      console.error("Unable to render dashboard history", error);
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async function renderHistoryDocument(
    history: DashboardHistoryDocument,
  ): Promise<void> {
    const first = history.samples[0];
    const last = history.samples.at(-1);
    if (first === undefined || last === undefined) {
      throw new DashboardSchemaError("dashboard history has no samples");
    }
    const { renderHistoryChart } = await import("./history-chart");
    setText(
      "history-package-change",
      formatChange(last.packages - first.packages),
    );
    setText("history-owner-change", formatChange(last.owners - first.owners));
    setText(
      "history-repository-change",
      formatChange(last.repositories - first.repositories),
    );
    renderHistoryChart(history.samples, {
      canvas: element<HTMLCanvasElement>("history-chart"),
      caption: element("history-caption"),
    });
    const rows = history.samples.map(historyRow);
    element<HTMLTableSectionElement>("history-values").replaceChildren(...rows);
  }

  function renderFailure(error: unknown): void {
    const invalid =
      error instanceof DashboardSchemaError ||
      (error instanceof DashboardLoadError && error.kind === "invalid");
    setStatus(
      "error",
      invalid ? "Published data is incompatible" : "Index snapshot unavailable",
      invalid
        ? "The current dashboard data could not be validated."
        : "The current dashboard data could not be loaded.",
    );
    statusLive.ariaBusy = "false";
    retry.hidden = false;
    content.hidden = true;
    console.error("Unable to render dashboard", error);
  }

  async function refreshDashboard(): Promise<void> {
    const currentRequest = ++requestNumber;
    setStatus(
      "loading",
      "Loading index snapshot",
      "Reading the current published data.",
    );
    statusLive.ariaBusy = "true";
    retry.hidden = true;
    content.hidden = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(
      () => controller.abort(),
      DASHBOARD_FETCH_TIMEOUT_MS,
    );
    try {
      const dashboard = await loadDashboard(dashboardUrl, {
        signal: controller.signal,
      });
      if (currentRequest === requestNumber) {
        renderDashboard(dashboard, currentRequest);
      }
    } catch (error) {
      if (currentRequest === requestNumber) {
        renderFailure(error);
      }
    } finally {
      window.clearTimeout(timeout);
    }
  }

  retry.addEventListener("click", () => void refreshDashboard());
  void refreshDashboard();
}

function historyRow(sample: DashboardHistorySample): HTMLTableRowElement {
  const row = document.createElement("tr");
  const date = document.createElement("th");
  date.scope = "row";
  date.textContent = formatPublicationDate(sample.date);
  row.append(
    date,
    numericCell(sample.packages),
    numericCell(sample.owners),
    numericCell(sample.repositories),
    numericCell(sample.size_known_packages),
    numericCell(sample.downloads_known_packages),
  );
  return row;
}

function numericCell(value: number): HTMLTableCellElement {
  const cell = document.createElement("td");
  cell.className = "numeric";
  cell.textContent = formatCount(value);
  return cell;
}

function formatChange(value: number): string {
  if (value > 0) {
    return `+${formatCount(value)}`;
  }
  return formatCount(value);
}

function historyPeriod(samples: ReadonlyArray<DashboardHistorySample>): string {
  if (samples.length === 1) {
    return "First daily sample";
  }
  return `${formatCount(samples.length)} daily samples`;
}

function requiredDashboardUrl(): string {
  const value = document.body.dataset.dashboardUrl;
  if (value === undefined) {
    throw new Error("Dashboard URL is missing");
  }
  return value;
}

function element<T extends Element = HTMLElement>(id: string): T;
function element(id: string): Element {
  const found = document.getElementById(id);
  if (found === null) {
    throw new Error(`Dashboard element is missing: ${id}`);
  }
  return found;
}

function setText(id: string, value: string): void {
  element(id).textContent = value;
}

function coverageMeasure(basisPoints: number): HTMLDivElement {
  const measure = document.createElement("div");
  measure.className = "measure";
  const progress = document.createElement("progress");
  progress.max = 100;
  progress.value = basisPoints / 100;
  progress.setAttribute("aria-label", `${formatCoverage(basisPoints)} coverage`);
  const value = document.createElement("span");
  value.className = "measure-value";
  value.ariaHidden = "true";
  value.textContent = formatCoverage(basisPoints);
  measure.append(progress, value);
  return measure;
}

function packageTypeLabel(value: string): string {
  const normalized = value.replaceAll(/[_-]+/g, " ");
  return normalized.charAt(0).toUpperCase() + normalized.slice(1);
}
