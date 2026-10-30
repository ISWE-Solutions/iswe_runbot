// A failed build explains itself: "Why it failed" first, a fix hint, and a jump into the log.
const { test, expect } = require("@playwright/test");
const { failedBuildIds } = require("./helpers");

test("failed build: Why it failed, How to fix, Jump to the log", async ({ page }) => {
  const ids = await failedBuildIds(page);
  test.skip(ids.length === 0, "no failed build on the board");
  await page.goto(`/builds/${ids[0]}`);
  const why = page.locator('[data-tab-panel="build:why"]');
  await expect(why).toBeVisible(); // default tab for failed builds
  await expect(why).toContainText("Why it failed");
  await expect(why.getByText("How to fix")).toBeVisible();

  const jump = why.getByRole("button", { name: /Jump to this line in the log/ });
  if (await jump.count()) {
    await jump.first().click();
    await expect(page.locator('[data-tab-panel="build:log"]')).toBeVisible();
    await expect(page.locator('[data-tabs-group="build"] [role=tab][data-tab="log"]')).toHaveAttribute("aria-selected", "true");
  }
});
