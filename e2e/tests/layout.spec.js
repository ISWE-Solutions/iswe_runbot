// Every main page loads, fits the screen at phone/laptop/wide widths, and its tabs work.
const { test, expect } = require("@playwright/test");
const { firstLink, walkServerTabs } = require("./helpers");

const STATIC_PAGES = ["/", "/projects", "/admin/system", "/admin/github-accounts", "/admin/versions",
  "/admin/users", "/try", "/combos/new"];

async function discoverPages(page) {
  const pages = [...STATIC_PAGES];
  const project = await firstLink(page, "/projects?tab=projects", /^\/projects\/\d+$/);
  const combo = await firstLink(page, "/projects?tab=combined", /^\/combos\/\d+$/);
  const branch = await firstLink(page, "/?project=all", /^\/branches\/\d+$/);
  const build = await firstLink(page, "/?project=all", /^\/builds\/\d+$/);
  for (const p of [project, combo, branch, build]) if (p) pages.push(p);
  return pages;
}

test("all main pages load and fit at 390, 1440 and 1920px", async ({ page }) => {
  test.setTimeout(180_000);
  const pages = await discoverPages(page);
  const problems = [];
  for (const width of [390, 1440, 1920]) {
    await page.setViewportSize({ width, height: 900 });
    for (const path of pages) {
      const r = await page.goto(path);
      if (r.status() !== 200) problems.push(`${path} at ${width}px: HTTP ${r.status()}`);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      if (overflow > 1) problems.push(`${path} at ${width}px scrolls sideways by ${overflow}px`);
    }
  }
  expect(problems).toEqual([]);
});

test("Projects page tabs", async ({ page }) => {
  await page.goto("/projects");
  expect(await walkServerTabs(page)).toEqual(["projects", "combined", "plain"]);
});

test("a project page's tabs", async ({ page }) => {
  const p = await firstLink(page, "/projects?tab=projects", /^\/projects\/\d+$/);
  test.skip(!p, "no project");
  await page.goto(p);
  expect(await walkServerTabs(page)).toEqual(expect.arrayContaining(["branches", "deps", "settings"]));
});

test("a combined preview's tabs", async ({ page }) => {
  const c = await firstLink(page, "/projects?tab=combined", /^\/combos\/\d+$/);
  test.skip(!c, "no combined preview");
  await page.goto(c);
  expect(await walkServerTabs(page)).toEqual(expect.arrayContaining(["preview", "builds", "settings"]));
});

test("a branch page's tabs", async ({ page }) => {
  const b = await firstLink(page, "/?project=all", /^\/branches\/\d+$/);
  test.skip(!b, "no branch");
  await page.goto(b);
  expect(await walkServerTabs(page)).toEqual(expect.arrayContaining(["overview", "builds", "plan"]));
});

test("a build page's tabs (client-side)", async ({ page }) => {
  const b = await firstLink(page, "/?project=all", /^\/builds\/\d+$/);
  test.skip(!b, "no build");
  await page.goto(b);
  const keys = await page.$$eval('[data-tabs-group="build"] [role=tab]', (ts) => ts.map((t) => t.dataset.tab));
  expect(keys).toEqual(expect.arrayContaining(["log", "details"]));
  for (const key of keys) {
    await page.click(`[data-tabs-group="build"] [role=tab][data-tab="${key}"]`);
    await expect(page.locator(`[data-tab-panel="build:${key}"]`)).toBeVisible();
    for (const other of keys.filter((k) => k !== key)) {
      await expect(page.locator(`[data-tab-panel="build:${other}"]`)).toBeHidden();
    }
  }
});

test("branch settings keep typed input across tabs", async ({ page }) => {
  const b = await firstLink(page, "/?project=all", /^\/branches\/\d+$/);
  test.skip(!b, "no branch");
  await page.goto(`${b}/settings`);
  const tabs = page.locator('[data-tabs-group="bset"] [role=tab]');
  test.skip(await tabs.count() < 2, "settings page has a single section");
  const keys = await tabs.evaluateAll((ts) => ts.map((t) => t.dataset.tab));
  for (const key of keys) {
    await page.click(`[data-tabs-group="bset"] [role=tab][data-tab="${key}"]`);
    await expect(page.locator(`[data-tab-panel="bset:${key}"]`)).toBeVisible();
  }
  // All sections stay in one form, so hidden tabs still submit.
  const forms = await page.$$eval('[data-tab-panel^="bset:"]', (ps) => [...new Set(ps.map((p) => p.closest("form")?.action))]);
  expect(forms).toHaveLength(1);
});
