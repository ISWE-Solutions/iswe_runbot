// Opt-in (E2E_WRITE=1): creates a throwaway plain Odoo preview, opens it, then deletes it.
const { test, expect } = require("@playwright/test");

test.describe("Try Odoo round trip", () => {
  test.skip(process.env.E2E_WRITE !== "1", "set E2E_WRITE=1 to run tests that create things");

  test("create, open, log in to Odoo, delete", async ({ page }) => {
    test.setTimeout(8 * 60_000);
    const name = `e2e-${Date.now()}`;
    await page.goto("/try");
    await page.locator('input[name=default_odoo_version][value="18.0"]').check({ force: true });
    await page.locator('input[name=edition][value="community"]').check({ force: true });
    await page.locator('input[name=install_demo_data][value="without"]').check({ force: true });
    await page.fill("input[name=name]", name);
    await page.fill("input[name=slug]", name);
    await Promise.all([page.waitForURL(/\/combos\/\d+/), page.click('button:has-text("Create and build")')]);
    const detail = page.url().split("?")[0];

    // Wait for the build (the worker syncs, then builds; a ready-made database makes it ~20 s).
    const header = page.locator("h1").locator("xpath=..");
    await expect(async () => {
      await page.goto(`${detail}?tab=preview`);
      const text = await header.innerText();
      expect(text).not.toMatch(/failed/);
      expect(text).toMatch(/running/);
    }).toPass({ timeout: 5 * 60_000, intervals: [5_000] });

    // Open it: preview pass (already signed in), then Odoo's own login.
    const host = `http://${name}.${new URL(page.url()).host}`;
    await page.goto(`${host}/web/login`);
    await expect(page).toHaveURL(new RegExp(`^${host}/web/login`));
    await page.fill("input[name=login]", "admin");
    await page.fill("input[name=password]", "admin");
    await Promise.all([page.waitForURL(/\/odoo/, { timeout: 60_000 }), page.click("button[type=submit]")]);

    // Delete with the typed confirmation.
    await page.goto(`${detail}?tab=settings`);
    await page.fill("input[name=confirm_slug]", name);
    await Promise.all([page.waitForURL(/\/projects/), page.click('button:has-text("Delete")')]);
    await expect(async () => {
      const r = await page.request.get(`${host}/web/login`, { maxRedirects: 0 });
      expect(r.status()).toBe(404);
    }).toPass({ timeout: 90_000, intervals: [3_000] });
  });
});
