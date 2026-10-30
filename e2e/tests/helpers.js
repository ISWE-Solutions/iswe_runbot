// Shared helpers. Selectors follow the app's markup: the login button has no type=submit,
// server-side tabs are `nav a[data-tab]` (+ aria-current), client-side tabs `[role=tab][data-tab]`
// with panels `[data-tab-panel="<group>:<key>"]`.
const { expect } = require("@playwright/test");

const EMAIL = process.env.E2E_EMAIL || "admin@preview.localhost"; // local dev login only
const PASSWORD = process.env.E2E_PASSWORD || "Preview-Admin-2026";

async function login(page, email = EMAIL, password = PASSWORD) {
  await page.goto("/login");
  await page.fill("input[name=email]", email);
  await page.fill("input[name=password]", password);
  await Promise.all([page.waitForURL((u) => !u.pathname.startsWith("/login")), page.click("main form button")]);
}

async function noHorizontalScroll(page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow, "page scrolls sideways").toBeLessThanOrEqual(1);
}

// Preview hosts the board links to with "Open preview" (running previews only).
async function runningPreviews(page) {
  await page.goto("/?project=all");
  return page.$$eval('a[title^="Open preview"]', (as) => [...new Set(as.map((a) => a.href))]);
}

// Build ids of failed build cards on the board.
async function failedBuildIds(page) {
  await page.goto("/?project=all");
  return page.$$eval('span[title="failed"]', (spans) =>
    [...new Set(spans.map((s) => s.parentElement?.querySelector('a[href^="/builds/"]')?.getAttribute("href"))
      .filter(Boolean).map((h) => h.split("/").pop()))]);
}

// First href on `path` matching `re` (e.g. the first project or combined preview).
async function firstLink(page, path, re) {
  await page.goto(path);
  const hrefs = await page.$$eval("a[href]", (as) => as.map((a) => a.getAttribute("href")));
  return hrefs.find((h) => re.test(h));
}

// Text of the main area below the tab bar, used to check that switching tabs changes content.
async function mainText(page) {
  return (await page.locator("main").innerText()).replace(/\s+/g, " ");
}

// Click through every server-side tab on the current page; each must load its own URL,
// become the current tab and change the content.
async function walkServerTabs(page) {
  const keys = await page.$$eval("nav a[data-tab]", (as) => as.map((a) => a.dataset.tab));
  let previous = await mainText(page);
  const visited = [];
  for (const key of keys) {
    const tab = page.locator(`nav a[data-tab="${key}"]`).first();
    if (await tab.getAttribute("aria-current") === "page") { visited.push(key); continue; }
    await Promise.all([page.waitForURL(new RegExp(`[?&]tab=${key}`)), tab.click()]);
    await expect(page.locator(`nav a[data-tab="${key}"]`).first()).toHaveAttribute("aria-current", "page");
    const now = await mainText(page);
    expect(now, `tab ${key} shows the same content as the previous tab`).not.toBe(previous);
    previous = now;
    visited.push(key);
  }
  return visited;
}

module.exports = { EMAIL, PASSWORD, login, noHorizontalScroll, runningPreviews, failedBuildIds, firstLink, walkServerTabs, mainText };
