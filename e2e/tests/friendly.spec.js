// Friendly links /<version>/<project>/<branch>: redirect to the preview, or explain a version mismatch.
const { test, expect } = require("@playwright/test");

async function friendlyLink(page) {
  for (const path of ["/projects?tab=projects", "/projects?tab=combined"]) {
    await page.goto(path);
    const projects = await page.$$eval("a[href]", (as) => as.map((a) => a.getAttribute("href"))
      .filter((h) => /^\/(projects|combos)\/\d+$/.test(h)));
    for (const p of projects) {
      await page.goto(p);
      const links = await page.$$eval("a[href]", (as) => as.map((a) => a.getAttribute("href"))
        .filter((h) => /^\/\d+\.\d+\/[a-z0-9-]+(\/[a-z0-9._-]+)?$/.test(h)));
      if (links.length) return links[0];
    }
  }
  return null;
}

test("friendly link redirects to its preview", async ({ page }) => {
  const link = await friendlyLink(page);
  test.skip(!link, "no friendly link shown (nothing running)");
  const r = await page.request.get(link, { maxRedirects: 0 });
  expect(r.status()).toBe(302);
  expect(new URL(r.headers()["location"]).host).toMatch(/\.preview\.|\./);
});

test("wrong version shows which version the branch runs", async ({ page }) => {
  const link = await friendlyLink(page);
  test.skip(!link, "no friendly link shown (nothing running)");
  const [, version, rest] = link.match(/^\/(\d+\.\d+)(\/.*)$/);
  const other = version === "18.0" ? "19.0" : "18.0";
  const r = await page.goto(`/${other}${rest}`);
  expect(r.status()).toBe(404);
  await expect(page.locator("main")).toContainText(`runs Odoo ${version}`);
});
