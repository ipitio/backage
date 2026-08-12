import { expect, test, type Page } from "@playwright/test";

import {
  dashboardFixture,
  historyFixture,
  utcDate,
} from "../dashboard-fixtures.ts";

const candidate = "/.bkg-site/candidate/index.html";
const releaseUrl = "https://github.com/example/backage/releases/latest";

test("shows a useful loading state before current data arrives", async ({ page }) => {
  let releaseDashboard: () => void = () => undefined;
  const pendingDashboard = new Promise<void>((resolve) => {
    releaseDashboard = resolve;
  });
  await page.route("**/dashboard.json", async (route) => {
    await pendingDashboard;
    await route.fulfill({ json: dashboardFixture() });
  });
  await page.route("**/dashboard-history.json", (route) =>
    route.fulfill({ json: historyFixture() }),
  );

  await page.goto(candidate);
  await expect(page.locator("#status-title")).toHaveText("Loading index snapshot");
  await expect(page.getByRole("link", { name: "Latest release" })).toHaveAttribute(
    "href",
    releaseUrl,
  );

  releaseDashboard();
  await expect(page.locator("#status-title")).toHaveText("Index snapshot current");
});

test("renders current inventory, accessible history, and repository navigation", async ({
  page,
}) => {
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  await routeSuccess(page);

  await page.goto(candidate);

  await expect(page.locator("#status-title")).toHaveText("Index snapshot current");
  await expect(page.locator("#inventory-packages")).toHaveText("1,200");
  await expect(page.locator("#history-status")).toHaveText("3 daily samples");
  await expect(page.locator("#history-package-change")).toHaveText("+20");
  await expect(page.locator("#history-line")).not.toHaveAttribute("points", "");
  await expect(page.locator("#history-chart-description")).toContainText(
    "Package count changed from 1,180",
  );
  await expect
    .poll(() =>
      page.locator(".brand img").evaluate(
        (image) =>
          image instanceof HTMLImageElement &&
          image.complete &&
          image.naturalWidth > 0,
      ),
    )
    .toBe(true);
  await expect(page.getByRole("link", { name: "Latest release" })).toHaveAttribute(
    "href",
    releaseUrl,
  );
  const details = page.locator(".history-details");
  await details.locator("summary").focus();
  await page.keyboard.press("Enter");
  await expect(details).toHaveAttribute("open", "");
  await expect(page.locator("#history-values tr")).toHaveCount(3);
  expect(consoleErrors).toEqual([]);
});

test("keeps navigation and retry available for incompatible data", async ({ page }) => {
  await page.route("**/dashboard.json", (route) => route.fulfill({ json: {} }));

  await page.goto(candidate);

  await expect(page.locator("#status-title")).toHaveText(
    "Published data is incompatible",
  );
  await expect(page.getByRole("button", { name: "Retry" })).toBeVisible();
  await expect(page.locator("#dashboard-content")).toBeHidden();
  await expect(page.getByRole("link", { name: "Latest release" })).toHaveAttribute(
    "href",
    releaseUrl,
  );
});

test("labels an old but valid projection as stale", async ({ page }) => {
  const staleDate = utcDate(-2);
  await routeSuccess(page, staleDate);

  await page.goto(candidate);

  await expect(page.locator("#status-title")).toHaveText(
    "Index snapshot may be stale",
  );
  await expect(page.locator("#dashboard-content")).toBeVisible();
});

test("recovers from a network failure through retry", async ({ page }) => {
  let attempts = 0;
  await page.route("**/dashboard.json", async (route) => {
    attempts += 1;
    if (attempts === 1) {
      await route.abort("failed");
      return;
    }
    await route.fulfill({ json: dashboardFixture() });
  });
  await page.route("**/dashboard-history.json", (route) =>
    route.fulfill({ json: historyFixture() }),
  );

  await page.goto(candidate);
  await expect(page.locator("#status-title")).toHaveText(
    "Index snapshot unavailable",
  );
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.locator("#status-title")).toHaveText("Index snapshot current");
  expect(attempts).toBe(2);
});

test("keeps current totals when optional history fails", async ({ page }) => {
  await page.route("**/dashboard.json", (route) =>
    route.fulfill({ json: dashboardFixture() }),
  );
  await page.route("**/dashboard-history.json", (route) => route.abort("failed"));

  await page.goto(candidate);

  await expect(page.locator("#status-title")).toHaveText("Index snapshot current");
  await expect(page.locator("#inventory-packages")).toHaveText("1,200");
  await expect(page.locator("#history-status")).toHaveText("History unavailable");
  await expect(page.locator("#history-unavailable")).toBeVisible();
});

test("provides raw data and release navigation without JavaScript", async ({ browser }) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();

  await page.goto(candidate);

  await expect(page.locator("#publication-status")).toBeHidden();
  await expect(page.getByRole("heading", { name: "Dashboard data requires JavaScript" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Latest release" })).toHaveAttribute(
    "href",
    releaseUrl,
  );
  await expect(page.getByRole("link", { name: "dashboard.json" })).toBeVisible();
  await context.close();
});

for (const viewport of [
  { name: "wide", width: 1_280, height: 900, inventoryColumns: 4, distributionColumns: 2 },
  { name: "narrow", width: 390, height: 844, inventoryColumns: 1, distributionColumns: 1 },
] as const) {
  test(`keeps the ${viewport.name} layout contained and readable`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await routeSuccess(page);
    await page.goto(candidate);
    await expect(page.locator("#status-title")).toHaveText("Index snapshot current");

    const bodyContained = await page.locator("body").evaluate(
      (body) => body.scrollWidth <= body.clientWidth,
    );
    expect(bodyContained).toBe(true);
    expect(await gridColumns(page, ".inventory-grid")).toBe(
      viewport.inventoryColumns,
    );
    expect(await gridColumns(page, ".distribution-grid")).toBe(
      viewport.distributionColumns,
    );
    if (viewport.name === "narrow") {
      expect(
        await page.locator(".distribution-grid .table-scroll").evaluateAll(
          (tables) =>
            tables.every((table) => table.scrollWidth <= table.clientWidth),
        ),
      ).toBe(true);
    }
  });
}

test.describe("dark mode", () => {
  test.use({ colorScheme: "dark" });

  test("uses the dark palette without changing dashboard behavior", async ({ page }) => {
    await routeSuccess(page);
    await page.goto(candidate);

    await expect(page.locator("#status-title")).toHaveText("Index snapshot current");
    await expect(page.locator("body")).toHaveCSS("background-color", "rgb(23, 27, 24)");
  });
});

async function routeSuccess(page: Page, generatedDate = utcDate()): Promise<void> {
  await page.route("**/dashboard.json", (route) =>
    route.fulfill({ json: dashboardFixture(generatedDate) }),
  );
  await page.route("**/dashboard-history.json", (route) =>
    route.fulfill({ json: historyFixture(generatedDate) }),
  );
}

async function gridColumns(page: Page, selector: string): Promise<number> {
  return page.locator(selector).evaluate((element) => {
    const columns = getComputedStyle(element).gridTemplateColumns;
    return columns.split(" ").filter(Boolean).length;
  });
}
