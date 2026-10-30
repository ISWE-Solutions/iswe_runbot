// Board: project tabs, branch filters, search, counters, responsive layout.
const { test, expect } = require("@playwright/test");
const { noHorizontalScroll } = require("./helpers");

test("project tabs switch the board", async ({ page }) => {
  await page.goto("/?project=all");
  const tabs = page.locator('.tabs-bar nav[aria-label="Projects"] a');
  const names = await tabs.allInnerTexts();
  expect(names[0].trim()).toBe("All");
  for (let i = 1; i < names.length; i++) {
    await Promise.all([page.waitForURL(/[?&]project=/), tabs.nth(i).click()]);
    await expect(tabs.nth(i)).toHaveClass(/border-indigo-600/);
  }
});

test("filters: All, Main branches, Work branches", async ({ page }) => {
  await page.goto("/?project=all");
  for (const [label, show] of [["Main branches", "sticky"], ["Work branches", "dev"], ["All", "all"]]) {
    const link = page.locator(`a[href*="show=${show}"]`).first();
    await expect(link).toContainText(label);
    await Promise.all([page.waitForURL(new RegExp(`show=${show}`)), link.click()]);
  }
  await expect(page.locator('a[href*="show=sticky"]').first()).toHaveAttribute("title", /Starred/);
});

test("search narrows the rows", async ({ page }) => {
  await page.goto("/?project=all");
  await page.fill('input[name="q"]', "zz-no-such-branch-zz");
  await Promise.all([page.waitForURL(/q=zz-no-such-branch-zz/), page.click('form[action="/"] button')]);
  await expect(page.getByText("No branches match these filters.")).toBeVisible();
  const rows = await page.$$eval("a[href]", (as) => as.map((a) => a.getAttribute("href"))
    .filter((h) => /^\/(branches|combos)\/\d+$/.test(h)));
  expect(rows).toEqual([]);
});

test("counters are shown", async ({ page }) => {
  await page.goto("/?project=all");
  for (const c of ["Queued", "Building", "Running", "Failed"]) {
    await expect(page.locator("#board-counters")).toContainText(`${c}:`);
  }
});

for (const width of [1440, 390]) {
  test(`board fits at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/?project=all");
    await noHorizontalScroll(page);
  });
}
