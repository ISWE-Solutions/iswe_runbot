// End-to-end tests against a running previewer stack (see e2e/README.md).
// Read-only by default; tests that create things need E2E_WRITE=1.
const { defineConfig } = require("@playwright/test");

const launchOptions = process.env.E2E_CHROME ? { executablePath: process.env.E2E_CHROME } : {};

module.exports = defineConfig({
  testDir: "./tests",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  retries: 0,
  workers: 1, // one shared live stack
  reporter: [["list"], ["html", { outputFolder: "report", open: "never" }]],
  outputDir: "test-results",
  use: {
    baseURL: process.env.E2E_BASE_URL || "http://preview.localhost",
    headless: true,
    viewport: { width: 1440, height: 900 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    launchOptions,
  },
  projects: [
    { name: "setup", testMatch: /auth\.setup\.js/ },
    {
      name: "e2e",
      testIgnore: /auth\.setup\.js/,
      dependencies: ["setup"],
      use: { storageState: ".auth/admin.json" },
    },
  ],
});
