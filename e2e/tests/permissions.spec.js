// A tester can look but not change things. Needs E2E_TESTER_EMAIL / E2E_TESTER_PASSWORD.
const { test, expect } = require("@playwright/test");
const { login } = require("./helpers");

const email = process.env.E2E_TESTER_EMAIL, password = process.env.E2E_TESTER_PASSWORD;

test.describe("tester", () => {
  test.skip(!email || !password, "set E2E_TESTER_EMAIL and E2E_TESTER_PASSWORD");
  test.use({ storageState: { cookies: [], origins: [] } });

  test("sees no Rebuild, Delete or settings Save", async ({ page }) => {
    await login(page, email, password);
    await page.goto("/?project=all");
    await expect(page.locator('[title^="Rebuild"]')).toHaveCount(0);
    await expect(page.locator('[title^="Delete"]')).toHaveCount(0);
    const build = await page.$$eval('a[href^="/builds/"]', (as) => as[0]?.getAttribute("href"));
    if (build) {
      await page.goto(build);
      await expect(page.getByRole("button", { name: /Delete build/ })).toHaveCount(0);
    }
    await page.goto("/admin/system?tab=settings");
    await expect(page.getByRole("button", { name: /Save settings/ })).toHaveCount(0);
  });
});
