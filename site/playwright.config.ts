import { defineConfig } from "@playwright/test";

const baseURL = "http://127.0.0.1:4173";

export default defineConfig({
  forbidOnly: true,
  fullyParallel: true,
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
  reporter: "line",
  retries: 0,
  testDir: "./tests/browser",
  timeout: 15_000,
  use: {
    baseURL,
    colorScheme: "light",
    locale: "en-US",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "node tests/server.ts",
    reuseExistingServer: false,
    timeout: 10_000,
    url: `${baseURL}/.bkg-site/candidate/index.html`,
  },
  workers: 2,
});
