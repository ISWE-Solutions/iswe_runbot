# Odoo Branch Preview System: Build Spec for Claude Code

Oct 30, 2026 · @Thom Simbeye

## How to use this spec

Download this doc as Markdown, save it as `SPEC.md` in an empty project folder, and tell Claude Code: "Read SPEC.md and build Phase 1."

Instructions for Claude Code:

- Read the whole spec before writing any code. Ask about anything under "Open decisions" that blocks the current phase.
- Propose a plan and folder structure first, and wait for approval before building.
- Build one phase at a time. Each phase ends with working code, tests, an updated README, and a run of that phase's acceptance checks.
- Prefer simple, proven tools. One server, Docker Compose, no Kubernetes.
- Keep all settings in one `.env` file (with a committed `.env.example`). Never commit secrets.
- Create and maintain a `CLAUDE.md` with the project's conventions, commands, and folder layout.

## Goal and overview

Build a self-hosted web app that turns every branch of a registered GitHub repo into a running Odoo instance with its own web address, like Odoo's Runbot but for our custom modules.

Expected load: 5 developers, 3 testers, about 5 projects with about 6 branches each, so roughly 30 branch previews.

Core flow:

1. An admin adds a project in the dashboard: the GitHub repo URL (for example `github.com/acme/custom-payroll`), an access token, and a default Odoo version.
2. The system discovers the repo's branches.
3. Each branch uses the project's default Odoo version unless someone sets a different version for that branch in the dashboard.
4. For each branch, the system starts a Docker container running the chosen Odoo version with that branch's code, on a fresh database with the custom modules installed.
5. Each preview gets its own web address, linked from the dashboard.
6. New commits trigger a rebuild. Deleted branches are cleaned up automatically.

Out of scope: hosting production systems, and running automated tests (GitHub Actions already does that).

## Routing design

Each preview runs on its own subdomain. The path style `base_url/<version>/<project>/<branch>` is kept as a friendly link that redirects to that subdomain.

Why Odoo is not served directly under a path:

- Odoo assumes it sits at the root of a domain. Its login, assets (`/web/assets/...`) and live updates (`/websocket`) use absolute paths, so `/18.0/payroll/...` breaks pages.
- Previews sharing one domain would share the `session_id` cookie, so testers would be logged out each time they switch previews.
- A project has several branches, so the address must include the branch.

| Address | Example | Purpose |
| --- | --- | --- |
| Dashboard | `https://preview.example.com/` | Manage projects, branches and builds |
| Preview (real) | `https://payroll-feature-tax-fix.preview.example.com` | The running Odoo for one branch |
| Friendly link | `https://preview.example.com/18.0/payroll/feature-tax-fix` | Redirects (HTTP 302) to the real preview address |

Rules:

- Preview host = `<project-slug>-<branch-slug>.preview.<domain>`. The app stores the host-to-build mapping, so a hyphen in names is not a problem. Reject a new branch whose host would clash with an existing one and show it in the dashboard.
- Slugs: lowercase, every character outside `a-z0-9` becomes `-`, repeated hyphens collapse, no leading or trailing hyphen. The full host label must be at most 63 characters; if longer, truncate and append a 6-character hash.
- Friendly link with a version that does not match the branch's current version: show a short page saying which version the branch runs, with a link to it.
- Friendly link for a branch with no running build: show the branch status page in the dashboard.
- DNS: one wildcard record `*.preview.<domain>` pointing to the server. HTTPS via a wildcard Let's Encrypt certificate using the DNS-01 challenge (needs the DNS provider's API token).

## Architecture and tech stack

Everything runs on one server under Docker Compose. Traefik receives all web traffic and sends dashboard requests to the app and preview requests to the matching Odoo container.

| Component | Technology | Responsibility |
| --- | --- | --- |
| Reverse proxy | Traefik v3 | HTTPS, wildcard certificate, routing subdomains to containers via Docker labels, JSON access log |
| Docker socket proxy | `tecnativa/docker-socket-proxy` | Gives Traefik read-only access to Docker instead of the raw socket |
| Backend API | Python 3.12, FastAPI, SQLAlchemy, Alembic | Dashboard API, GitHub sync and webhooks, auth, friendly-link redirects |
| Build worker | Same Python codebase, separate process, Docker SDK (`docker` package) | Runs queued jobs: build, start, stop, wake, delete |
| Job queue | A `jobs` table in Postgres (`SELECT ... FOR UPDATE SKIP LOCKED`) | Avoids adding Redis |
| Frontend | Server-rendered Jinja templates + HTMX, Tailwind CSS | Dashboard pages, live status and log streaming |
| Database server | PostgreSQL 16 (one container) | Holds the app's own database and one database per preview |
| Preview containers | Official `odoo:<version>` images | One container per running branch |
| Storage | Host folders under `/srv/previewer/` | `repos/` (git mirrors), `builds/` (checked-out code), `filestore/`, `logs/` |

Notes:

- One Postgres 16 server works for Odoo 16.0 to 19.0. If older versions are added later, allow a per-version Postgres container in the `odoo_versions` table.
- The backend tails Traefik's access log to record when each preview was last visited (used for sleep).
- Frontend choice is HTMX to keep one language. Switch to React only if asked.

## Data model

A project has many branches, a branch has many builds, and at most one build per branch is live at a time.

| Table | Key fields | Notes |
| --- | --- | --- |
| `projects` | name, slug, repo\_url, access\_token (encrypted), default\_odoo\_version, edition (community / enterprise), modules\_to\_install (blank = auto-detect), install\_demo\_data, branch\_include\_regex, branch\_exclude\_regex, extra\_addons\_repos (JSON list of url + branch) | Slug is unique and editable |
| `branches` | project\_id, name, slug, odoo\_version\_override (nullable), enabled, last\_commit\_sha, last\_commit\_message, last\_commit\_author, last\_commit\_at, deleted\_at | Effective version = override, else project default |
| `builds` | branch\_id, commit\_sha, odoo\_version, status, container\_id, db\_name, host, started\_at, finished\_at, last\_accessed\_at, log\_path, error\_summary | Keep the latest successful build plus recent failed ones |
| `odoo_versions` | version (`16.0`...`19.0`), docker\_image, demo\_flag\_on, demo\_flag\_off, enabled | Admin-managed. Demo flags differ by version, so store them here and verify each against `odoo --help` |
| `users` | email, name, password\_hash, role (admin / developer / tester), active |  |
| `jobs` | type (build / start / stop / wake / delete / sync), payload JSON, status, attempts, run\_after, error | Worker picks jobs in order |
| `settings` | key, value | Idle hours, limits, retention days |

Build status values: `queued`, `building`, `running`, `sleeping`, `failed`, `stopped`, `removed`.

Changing a branch's Odoo version always triggers a new build on a fresh database. Never try to upgrade a preview database between versions.

## Build and run pipeline

A build turns one commit into a running preview in seven steps, and the previous preview stays up until the new one is healthy.

Triggers: a GitHub webhook (`push`, `create`, `delete`) at `/api/webhooks/github/<project id>` with its HMAC signature verified before the body is parsed; polling every 5 minutes as a fallback; the Rebuild button; a change of Odoo version or branch settings.

1. **Fetch code.** Keep one bare git mirror per project in `repos/`. Fetch, then export the commit to `builds/<build-id>/src`. Fetch any `extra_addons_repos` the same way (default branch name = the Odoo version, e.g. `18.0`).
2. **Prepare image.** If the repo has `requirements.txt`, build a small image `FROM odoo:<version>` that installs it, tagged by a hash of version + requirements file so it is reused. Otherwise use `odoo:<version>` as is. Code is mounted read-only, never baked into the image.
3. **Find modules.** Use `modules_to_install` if set; else every folder one level deep containing `__manifest__.py`.
4. **Create database.** Name `<project>_<branch>_<short-sha>`, sanitised, max 63 characters. Run a one-off container: `odoo -d <db> -i <modules> --stop-after-init` with the addons path set to core + extra repos + source, and the version's demo flag. Time limit 20 minutes.
5. **Start preview.** Long-running container with `--database <db> --db-filter ^<db>$ --no-database-list --workers 0 --proxy-mode`, a random master password, 1.5 GB memory limit, 1 CPU, and Traefik labels for its host on port 8069. With `--workers 0`, the websocket is served on the same port.
6. **Health check.** Poll `/web/login` until it returns 200 (max 2 minutes). Mark the build `running`.
7. **Swap.** Stop the branch's previous build and drop its database and filestore.

Failure handling: mark the build `failed`, save the last ERROR and CRITICAL log lines as `error_summary`, and leave the previous running build untouched.

Concurrency: at most 3 builds at once (setting), others wait in order. A new commit on a branch cancels that branch's queued build.

Logs: write build and runtime logs to `logs/<build-id>.log` and stream them live to the dashboard.

Enterprise projects: add the Odoo Enterprise addons (from a configured private repo, at the version branch) to the addons path.

## Dashboard UI pages

The main screen is the project page: one row per branch with its Odoo version as an editable dropdown, a colored status, and an Open button.

| Page | Shows | Actions |
| --- | --- | --- |
| Projects (home) | One card per project: name, repo, default version, count of previews by status | Add project, open project |
| Add / edit project | Form: name, repo URL, token, default Odoo version, edition, modules to install, demo data, branch include/exclude filters, extra addon repos | Test connection, save, delete project |
| Project detail | Table of branches: name, Odoo version (dropdown), status badge, last commit (short sha, message, author, time), preview link, friendly link, last visited | Open, Rebuild, Stop, Wake, View logs, Branch settings, Enable/disable, Sync branches now |
| Branch settings | Version override, modules override, demo data, enabled | Save (triggers rebuild if needed) |
| Build logs | Live log stream for a build, plus past builds of the branch | Download log |
| Odoo versions (admin) | Versions, images, demo flags, enabled | Add, edit, disable, pull image |
| Users (admin) | Users and roles | Invite, change role, deactivate |
| System (admin) | CPU, RAM, disk use, running previews, build queue, settings | Change limits and idle/cleanup times |

Status colors: queued grey, building blue, running green, sleeping purple, failed red, stopped dark grey.

Permissions:

- **Tester:** view projects, open previews, wake, view logs.
- **Developer:** tester rights plus rebuild, stop, change branch version and settings.
- **Admin:** everything, including projects, versions, users and system settings.

Each preview row also shows the Odoo login to use (default `admin` / `admin` on a fresh database).

## Sleep, cleanup and resource limits

Idle previews sleep after 3 hours and wake when someone visits, so about 30 branches fit on a 32 GB server.

Sleep and wake:

- A running preview with no visits for `IDLE_SLEEP_HOURS` (default 3) is stopped. Its database and filestore are kept. Status becomes `sleeping`.
- Add a low-priority catch-all Traefik route for `*.preview.<domain>` pointing to the dashboard. Running previews have higher-priority routes, so only sleeping or missing previews hit the catch-all.
- The catch-all shows a "Waking up" page, queues a `wake` job, and refreshes every 5 seconds until the preview is up (target under 60 seconds).

Cleanup:

- Branch deleted on GitHub: remove container, database, filestore and code after a 24-hour grace period.
- Branch with no new commits for 30 days: stop it and mark it stale. Delete it after 60 days unless someone wakes it. Both values are settings.
- Keep only the latest successful build per branch. Keep failed build logs for 14 days.
- Prune unused Docker images weekly.

Limits (all settings):

| Limit | Default |
| --- | --- |
| Memory per preview | 1.5 GB |
| CPU per preview | 1 core |
| Builds at the same time | 3 |
| Previews running at the same time | 25 (when exceeded, the longest-idle preview sleeps first) |
| Build time limit | 20 minutes |
| Disk warning on System page | 80% used |

## Security

Only logged-in team members can reach the dashboard and the previews, and secrets never leave the server unencrypted.

- **Dashboard login:** email and password (bcrypt hashes), session cookie set on `.preview.<domain>`. GitHub sign-in can come later.
- **Preview access:** Traefik forward-auth checks the dashboard session before any preview request. A per-project switch can turn this off for client demos.
- **GitHub tokens:** fine-grained, read-only (contents and metadata). Stored encrypted with a key from `.env` (Fernet).
- **Webhooks:** reject any request whose HMAC signature does not match the project's webhook secret.
- **Docker access:** only the build worker talks to the Docker socket directly. Traefik goes through the read-only socket proxy.
- **Database:** Postgres is not exposed outside the Docker network. Each build connects as its own role (`pv_b<build id>`, no CREATEDB) that owns only its database; `CONNECT` on every preview database and the app database is revoked from `PUBLIC`. Each preview also sees only its own database in Odoo (`--db-filter`, `--no-database-list`) and has a random master password, so the Odoo database manager cannot be used.
- **Server firewall:** open ports 22, 80 and 443 only.
- **Data:** previews use fresh databases with sample data. Copies of real client databases are out of scope until an anonymisation step exists (Phase 3).

## Deployment and server

The whole system installs on one Ubuntu 24.04 server with 8 CPU cores, 32 GB RAM and a 300 GB SSD, using one `docker-compose.yml`.

- **Services in Compose:** `traefik`, `socket-proxy`, `postgres`, `app` (backend API), `worker` (build worker). Preview containers are created by the worker on a separate `previews` network (shared only with Traefik, Postgres and the worker), not listed in Compose.
- **DNS:** A records for `preview.<domain>` and `*.preview.<domain>` pointing to the server.
- **`.env` keys (at least):** `DOMAIN`, `ACME_EMAIL`, `DNS_PROVIDER`, `DNS_API_TOKEN`, `SECRET_KEY`, `ENCRYPTION_KEY`, `POSTGRES_PASSWORD`, `MAX_CONCURRENT_BUILDS`, `MAX_RUNNING_PREVIEWS`, `IDLE_SLEEP_HOURS`, `PREVIEW_MEMORY_LIMIT`, `ADMIN_EMAIL`.
- **Install:** a `scripts/install.sh` that checks Docker is installed, creates `/srv/previewer/` folders, runs database migrations, and creates the first admin user. The README walks through it step by step.
- **Backups:** nightly dump of the app's own database only. Preview databases are disposable and not backed up.
- **Health:** `/health` endpoint reporting database, Docker and queue status.

## Build phases and acceptance criteria

Build in three phases. Phase 1 alone gives a working Runbot-style preview for each branch.

| Phase | Scope | Done when |
| --- | --- | --- |
| 1. Core previews | Login, add project, manual branch sync, per-branch Odoo version, build and run pipeline, subdomain routing, friendly-link redirect, build logs | The `custom-payroll` repo is added; two branches run at the same time on different versions (e.g. 17.0 and 18.0); each opens through its friendly link and the `admin` login works; a broken branch shows `failed` with an error summary while its previous build stays up |
| 2. Automation | Webhooks and polling, auto rebuild on push, sleep and wake-on-visit, cleanup rules, roles and permissions, resource limits, System page, forward-auth on previews | A push starts a build within 1 minute of the webhook; an idle preview sleeps after the set time and wakes on visit in under 60 seconds; a deleted branch is fully removed after the grace period; a tester cannot rebuild |
| 3. Extras (optional) | Enterprise addons, extra addon repos (e.g. OCA), preview link posted as a GitHub commit status on pull requests, anonymised client database templates, Slack notifications | Agreed per feature before starting |

## Open decisions

Confirm these before Phase 1 starts; Claude Code should ask about any left unticked.

- [ ] Domain for previews (replace `preview.example.com` everywhere)
- [ ] DNS provider and API token, needed for the wildcard certificate
- [ ] Odoo versions to support at launch (suggested: 16.0, 17.0, 18.0, 19.0)
- [ ] Do any projects need Odoo Enterprise, and do we have access to its repo?
- [ ] GitHub only, or GitLab as well?
- [ ] Odoo login on previews: shared `admin` / `admin`, or a password per project?
- [ ] Should clients ever open previews without a dashboard login (client demos)?
