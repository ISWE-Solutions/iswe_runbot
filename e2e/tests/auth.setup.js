// Logs in once and saves the dashboard session for the other tests.
const { test: setup } = require("@playwright/test");
const { login } = require("./helpers");

setup("log in to the dashboard", async ({ page }) => {
  await login(page);
  await page.context().storageState({ path: ".auth/admin.json" });
});
