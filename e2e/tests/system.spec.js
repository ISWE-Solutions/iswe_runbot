// System page: summary cards on top, every tab renders.
const { test, expect } = require("@playwright/test");
const { walkServerTabs } = require("./helpers");

test("summary cards and every tab", async ({ page }) => {
  await page.goto("/admin/system");
  for (const card of ["Worker", "Disk", "Server", "Previews"]) {
    await expect(page.locator("main")).toContainText(new RegExp(card, "i"));
  }
  const tabs = await walkServerTabs(page);
  expect(tabs).toEqual(["previews", "queue", "tasks", "templates", "settings", "jobs"]);
  await page.goto("/admin/system?tab=settings");
  await expect(page.getByRole("button", { name: /Save settings/ })).toBeVisible();
});
