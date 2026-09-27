import {
  Chart,
  LineController,
  LineElement,
  LinearScale,
  PointElement,
  Tooltip,
} from "chart.js";

import {
  formatCount,
  formatPublicationDate,
  MILLISECONDS_PER_DAY,
  type DashboardHistorySample,
} from "./dashboard";
import {
  formatChange,
  HISTORY_METRICS,
  selectHistorySamples,
  type HistoryMetric,
  type HistoryPeriod,
} from "./history";

Chart.register(LineController, LineElement, LinearScale, PointElement, Tooltip);

interface HistoryChartElements {
  canvas: HTMLCanvasElement;
  caption: HTMLElement;
  metric: HTMLFieldSetElement;
  period: HTMLSelectElement;
  observation: HTMLInputElement;
  valueLabel: HTMLElement;
  value: HTMLElement;
  date: HTMLTimeElement;
  change: HTMLElement;
  sampleCount: HTMLElement;
  renderRows: (samples: ReadonlyArray<DashboardHistorySample>) => void;
}

export function renderHistoryChart(
  samples: ReadonlyArray<DashboardHistorySample>,
  elements: HistoryChartElements,
): () => void {
  const listeners = new AbortController();
  const selectedMetric =
    elements.metric.querySelector<HTMLInputElement>("input:checked")?.value;
  let metric: HistoryMetric =
    selectedMetric !== undefined && isHistoryMetric(selectedMetric)
      ? selectedMetric
      : "packages";
  let period: HistoryPeriod = isHistoryPeriod(elements.period.value)
    ? elements.period.value
    : "30";
  let visible = selectHistorySamples(samples, period);
  let chart: Chart<"line", { x: number; y: number }[]> | undefined;

  function selectObservation(index: number): void {
    const sample = visible[index];
    if (sample === undefined) {
      return;
    }
    elements.observation.value = String(index);
    elements.observation.setAttribute(
      "aria-valuetext",
      `${formatPublicationDate(sample.date)} UTC; ${formatCount(sample[metric])} ${HISTORY_METRICS[metric].toLowerCase()}`,
    );
    elements.valueLabel.textContent = HISTORY_METRICS[metric];
    elements.value.textContent = formatCount(sample[metric]);
    elements.date.dateTime = sample.date;
    elements.date.textContent = `${formatPublicationDate(sample.date)} UTC`;
  }

  function draw(): void {
    const first = visible[0]!;
    const last = visible.at(-1)!;
    chart?.destroy();
    const styles = getComputedStyle(elements.canvas);
    const color = (name: string): string =>
      styles.getPropertyValue(name).trim();
    const series = color(`--chart-${metric}`);
    const firstTime = Date.parse(`${first.date}T00:00:00.000Z`);
    const lastTime = Date.parse(`${last.date}T00:00:00.000Z`);
    const singlePadding = visible.length === 1 ? MILLISECONDS_PER_DAY / 2 : 0;

    chart = new Chart(elements.canvas, {
      type: "line",
      data: {
        datasets: [
          {
            label: HISTORY_METRICS[metric],
            data: visible.map((sample) => ({
              x: Date.parse(`${sample.date}T00:00:00.000Z`),
              y: sample[metric],
            })),
            borderColor: series,
            borderWidth: 2,
            pointBackgroundColor: series,
            pointBorderColor: color("--pico-background-color"),
            pointBorderWidth: 2,
            pointRadius: visible.length < 10 ? 4 : 2,
            pointHoverRadius: 6,
            pointHitRadius: 12,
            tension: 0,
            segment: {
              borderDash: (context) => {
                const start = context.p0.parsed.x;
                const end = context.p1.parsed.x;
                return start !== null &&
                  end !== null &&
                  end - start > MILLISECONDS_PER_DAY
                  ? [4, 4]
                  : undefined;
              },
            },
          },
        ],
      },
      options: {
        animation: false,
        maintainAspectRatio: false,
        normalized: true,
        parsing: false,
        responsive: true,
        interaction: { axis: "x", mode: "nearest", intersect: false },
        onHover: (_event, points) => {
          const point = points[0];
          if (point !== undefined) {
            selectObservation(point.index);
          }
        },
        onClick: (_event, points) => {
          const point = points[0];
          if (point !== undefined) {
            selectObservation(point.index);
          }
        },
        plugins: {
          tooltip: {
            displayColors: false,
            padding: 12,
            cornerRadius: 4,
            callbacks: {
              title: (items) => {
                const sample = visible[items[0]?.dataIndex ?? 0];
                return sample === undefined
                  ? ""
                  : `${formatPublicationDate(sample.date)} UTC`;
              },
              label: (item) =>
                `${HISTORY_METRICS[metric]}: ${formatCount(item.parsed.y ?? 0)}`,
            },
          },
        },
        scales: {
          x: {
            type: "linear",
            min: firstTime - singlePadding,
            max: lastTime + singlePadding,
            border: { display: false },
            grid: { display: false },
            ticks: {
              color: color("--pico-muted-color"),
              stepSize: MILLISECONDS_PER_DAY,
              maxTicksLimit: 6,
              maxRotation: 0,
              callback: (value) =>
                new Intl.DateTimeFormat("en-US", {
                  month: "short",
                  day: "numeric",
                  timeZone: "UTC",
                }).format(new Date(Number(value))),
            },
          },
          y: {
            min: visible.some((sample) => sample[metric] === 0) ? 0 : undefined,
            border: { display: false },
            grid: { color: color("--pico-muted-border-color") },
            ticks: {
              color: color("--pico-muted-color"),
              precision: 0,
              maxTicksLimit: 5,
              callback: (value) =>
                typeof value === "number" ? formatCount(value) : value,
            },
          },
        },
      },
    });
    elements.canvas.ariaLabel =
      `${HISTORY_METRICS[metric]} over time. ${HISTORY_METRICS[metric]} changed from ` +
      `${formatCount(first[metric])} on ${formatPublicationDate(first.date)} to ` +
      `${formatCount(last[metric])} on ${formatPublicationDate(last.date)}.`;
  }

  function updateWindow(): void {
    visible = selectHistorySamples(samples, period);
    const first = visible[0]!;
    const last = visible.at(-1)!;
    const change = last[metric] - first[metric];
    elements.change.textContent = formatChange(change);
    elements.change.dataset.direction =
      change < 0 ? "negative" : change > 0 ? "positive" : "flat";
    elements.observation.max = String(visible.length - 1);
    elements.observation.disabled = visible.length === 1;
    elements.sampleCount.textContent = formatCount(visible.length);
    elements.caption.textContent = `${formatPublicationDate(first.date)} to ${formatPublicationDate(last.date)} UTC`;
    elements.renderRows(visible);
    selectObservation(visible.length - 1);
    draw();
  }

  const options = { signal: listeners.signal };
  elements.metric.addEventListener(
    "change",
    (event) => {
      const input = event.target;
      if (
        input instanceof HTMLInputElement &&
        input.checked &&
        isHistoryMetric(input.value)
      ) {
        metric = input.value;
        updateWindow();
      }
    },
    options,
  );
  elements.period.addEventListener(
    "change",
    () => {
      const value = elements.period.value;
      if (isHistoryPeriod(value)) {
        period = value;
        updateWindow();
      }
    },
    options,
  );
  elements.observation.addEventListener(
    "input",
    () => {
      const index = Number(elements.observation.value);
      selectObservation(index);
      const point = chart?.getDatasetMeta(0).data[index];
      if (chart !== undefined && point !== undefined) {
        const active = [{ datasetIndex: 0, index }];
        chart.setActiveElements(active);
        chart.tooltip?.setActiveElements(active, { x: point.x, y: point.y });
        chart.update("none");
      }
    },
    options,
  );
  matchMedia("(prefers-color-scheme: dark)").addEventListener(
    "change",
    draw,
    options,
  );
  updateWindow();
  return () => {
    listeners.abort();
    chart?.destroy();
  };
}

function isHistoryMetric(value: string): value is HistoryMetric {
  return value === "packages" || value === "owners" || value === "repositories";
}

function isHistoryPeriod(value: string): value is HistoryPeriod {
  return value === "7" || value === "30" || value === "90" || value === "all";
}
