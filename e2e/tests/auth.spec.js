// Dashboard login/logout and login protection on previews (forward-auth + preview pass).
const { test, expect } = require("@playwright/test");
const { EMAIL, PASSWORD, login, runningPreviews } = require("./helpers");

test.describe("signed out", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("wrong password is refused", async ({ page }) => {
    await page.goto("/login");
    await page.fill("input[name=email]", EMAIL);
    await page.fill("input[name=password]", "not-the-password");
    await page.click("main form button");
    await expect(page.getByText("Wrong email or password.")).toBeVisible();
    await expect(page).toHaveURL(/\/login/);
  });

  test("log in and log out", async ({ page }) => {
    await login(page);
    await expect(page.locator("#sidebar")).toBeVisible();
    await page.locator('#sidebar button:has-text("Sign out")').click();
    await expect(page).toHaveURL(/\/login/);
    await page.goto("/admin/system");
    await expect(page).toHaveURL(/\/login\?next=/);
  });

  test("protected preview: login, then back to the preview, then Odoo admin/admin", async ({ page, browser }) => {
    // Find a running preview with the signed-in session from setup.
    const ctx = await browser.newContext({ storageState: ".auth/admin.json" });
    const hosts = await runningPreviews(await ctx.newPage());
    await ctx.close();
    test.skip(hosts.length === 0, "no running preview on the board");
    const preview = new URL(hosts[0]);

    await page.goto(new URL("/web/login", preview).href);
    await expect(page).toHaveURL(/\/login\?next=/); // sent to the dashboard login
    await page.fill("input[name=email]", EMAIL);
    await page.fill("input[name=password]", PASSWORD);
    await Promise.all([page.waitForURL((u) => u.host === preview.host, { timeout: 30_000 }), page.click("main form button")]);

    const cookies = await page.context().cookies();
    const session = cookies.find((c) => c.name === "previewer_session");
    const pass = cookies.find((c) => c.name === "previewer_pass");
    expect(pass, "preview pass cookie").toBeTruthy();
    expect(session?.domain.startsWith("."), "dashboard session must stay host-only").toBeFalsy();

    await page.fill("input[name=login]", "admin");
    await page.fill("input[name=password]", "admin");
    await Promise.all([page.waitForURL(/\/(odoo|web)(\/|$|\?|#)/, { timeout: 60_000 }), page.click("button[type=submit]")]);
    await expect(page).not.toHaveURL(/\/web\/login/);
  });
});
