import {
  CategoryScale,
  Chart,
  LineController,
  LineElement,
  LinearScale,
  PointElement,
} from "chart.js";

import {
  DashboardSchemaError,
  formatCount,
  formatPublicationDate,
  type DashboardHistorySample,
} from "./dashboard";

Chart.register(
  CategoryScale,
  LineController,
  LineElement,
  LinearScale,
  PointElement,
);

interface HistoryChartElements {
  canvas: HTMLCanvasElement;
  caption: HTMLElement;
}

interface ThemeListener {
  listener: () => void;
  media: MediaQueryList;
}

const themeListeners = new WeakMap<HTMLCanvasElement, ThemeListener>();

export function renderHistoryChart(
  samples: ReadonlyArray<DashboardHistorySample>,
  elements: HistoryChartElements,
): void {
  drawHistoryChart(samples, elements);

  const previous = themeListeners.get(elements.canvas);
  previous?.media.removeEventListener("change", previous.listener);
  const media = matchMedia("(prefers-color-scheme: dark)");
  const listener = (): void => drawHistoryChart(samples, elements);
  media.addEventListener("change", listener);
  themeListeners.set(elements.canvas, { listener, media });
}

function drawHistoryChart(
  samples: ReadonlyArray<DashboardHistorySample>,
  elements: HistoryChartElements,
): void {
  const first = samples[0];
  const last = samples.at(-1);
  if (first === undefined || last === undefined) {
    throw new DashboardSchemaError("dashboard history has no chart samples");
  }

  Chart.getChart(elements.canvas)?.destroy();
  const styles = getComputedStyle(elements.canvas);
  const color = (name: string): string => styles.getPropertyValue(name).trim();

  new Chart(elements.canvas, {
    type: "line",
    data: {
      labels: samples.map((sample) => formatPublicationDate(sample.date)),
      datasets: [
        {
          data: samples.map((sample) => sample.packages),
          borderColor: color("--pico-primary"),
          borderWidth: 3,
          label: "Packages",
          pointBackgroundColor: color("--pico-background-color"),
          pointBorderColor: color("--pico-primary"),
          pointBorderWidth: 3,
          pointRadius: ({ dataIndex }) =>
            dataIndex === samples.length - 1 ? 5 : 0,
          tension: 0,
        },
      ],
    },
    options: {
      animation: false,
      events: [],
      maintainAspectRatio: false,
      normalized: true,
      responsive: true,
      scales: {
        x: {
          border: { color: color("--pico-muted-border-color") },
          grid: { display: false },
          ticks: {
            autoSkip: true,
            color: color("--pico-muted-color"),
            maxRotation: 0,
            maxTicksLimit: 6,
          },
        },
        y: {
          border: { display: false },
          grid: { color: color("--pico-muted-border-color") },
          ticks: {
            color: color("--pico-muted-color"),
            callback: (value) =>
              typeof value === "number" ? formatCount(value) : value,
            maxTicksLimit: 5,
          },
        },
      },
    },
  });

  elements.canvas.ariaLabel =
    `Package count over time. Package count changed from ` +
    `${formatCount(first.packages)} on ${formatPublicationDate(first.date)} to ` +
    `${formatCount(last.packages)} on ${formatPublicationDate(last.date)}.`;
  elements.caption.textContent =
    `${formatPublicationDate(first.date)} to ` +
    `${formatPublicationDate(last.date)} UTC`;
}
