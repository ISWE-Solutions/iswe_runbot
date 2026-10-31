# End-to-end tests

Browser tests (Playwright) that run against a **running** previewer stack. They are read-only
by default, so they are safe to run on the live stack at any time.

## Install

```bash
cd e2e
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm ci   # no browser download; point E2E_CHROME at one you have
```

Without `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD`, `npx playwright install chromium` gets Playwright's own
browser instead, and `E2E_CHROME` is not needed.

## Run

```bash
E2E_CHROME=~/Library/Caches/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-mac-arm64/chrome-headless-shell \
  npx playwright test                 # read-only suite (about 1 minute)
E2E_WRITE=1 npx playwright test       # also the write test: creates, opens and deletes "e2e-<time>"
npx playwright show-report report     # HTML report; failures keep screenshots and traces
```

| Variable | Default | Use |
|---|---|---|
| `E2E_BASE_URL` | `http://preview.localhost` | Dashboard URL of the stack under test |
| `E2E_EMAIL` | `admin@preview.localhost` | Dashboard login of the stack under test |
| `E2E_PASSWORD` | required | Its password (never committed; export it in your shell) |
| `E2E_CHROME` | unset | Path to a Chromium binary to use instead of Playwright's own |
| `E2E_WRITE` | unset | `1` runs tests that create (and delete) their own objects |

## What is covered

- **auth:** wrong password, log in/out, a protected preview sends you to the dashboard login and
  back, the preview pass cookie (dashboard session stays host-only), Odoo `admin`/`admin`.
- **board:** project tabs, All / Main branches / Work branches, search, counters, fits at 1440/390 px.
- **layout:** every main page returns 200 and has no sideways scroll at 390/1440/1920 px; the
  tabs on Projects, a project, a combined preview, a branch, a build and branch settings switch
  their content.
- **failure:** a failed build opens on "Why it failed" with a "How to fix" hint; "Jump to this
  line in the log" switches to the Log tab.
- **friendly links:** redirect to the preview; a wrong version explains which version runs.
- **System:** the summary cards and all six tabs.
- **write** (`E2E_WRITE=1`): Try Odoo → plain Odoo 18 Community → wait until running → open it
  through the preview pass → Odoo login → delete with the typed confirmation → host returns 404.

Tests find what to test (running previews, failed builds, projects) from the board and the
Projects page, so they work on any stack with some data; they skip a check when there is
nothing to check (e.g. no failed build).
