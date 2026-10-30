# Odoo Branch Previewer

A self-hosted, Runbot-style service: every branch of a registered GitHub repo runs as its own
Odoo instance with its own web address, so developers and testers can open any branch in a
browser and log in.

- Dashboard: `https://preview.example.com/`
- A preview: `https://payroll-v18.preview.example.com` (`<project>-<branch>.<domain>`)
- Friendly link: `https://preview.example.com/18.0/payroll/v18` redirects to the preview

The full specification is in [SPEC.md](SPEC.md). **Status:** Phase 1 and Phase 2 are done,
plus the Phase 3 items Enterprise addons, extra addon repos, GitHub commit statuses and Slack
alerts. Still open: anonymised client database templates, and a real server and domain.

## How it works

```text
browser ──► Traefik ──► app (FastAPI dashboard)          ◄── reads DB + log files
              │                     │ enqueues jobs
              │                     ▼
              │               Postgres 16  ◄── app DB + one DB per preview
              │                     ▲
              │                     │ claims jobs (FOR UPDATE SKIP LOCKED)
              │               worker ──► Docker: odoo:<version> containers
              └──────────────────────────► pv-<build-id> (one per running branch)
```

A build turns one commit into a running preview:

1. Fetch the repo into a bare mirror and export the commit. Extra addon repos and Odoo
   Enterprise (for enterprise projects) are fetched the same way.
2. Use `odoo:<version>`, or a small derived image if the repo has `requirements.txt`.
3. Find the modules to install: the configured list, or every module in the repo.
4. Create a fresh database with `odoo -i <modules> --stop-after-init` (20 minute limit), then
   check that every module really installed.
5. Start the preview container: 1.5 GB RAM, 1 CPU, a random master password, its own database
   only, and the database manager disabled.
6. Wait until `/web/login` answers. The build is then `running`.
7. Swap: remove the branch's previous build, including its database and files.

If a build fails, it is marked `failed` with the last error lines, and the previous preview
stays up.

## Run it locally (macOS or Linux with Docker)

Previews are served on `*.preview.localhost` over plain HTTP. Browsers and curl resolve
`*.localhost` to your machine, so no DNS setup is needed. Port 80 must be free.

```bash
cp .env.example .env          # then fill in SECRET_KEY, ENCRYPTION_KEY, POSTGRES_PASSWORD,
                              # ODOO_DB_PASSWORD, DATA_DIR=<absolute path>/data, DOMAIN=preview.localhost
docker compose up -d --build  # also loads docker-compose.override.yml (live code reload)
docker compose exec app python -m previewer.cli create-user --email you@example.com --role admin
```

Open <http://preview.localhost> and sign in.

Pull the Odoo images once, or use **Odoo versions → Pull image** in the dashboard:

```bash
docker pull odoo:18 && docker pull odoo:19
```

## Add a project

**Projects → Add project**, then **Test connection**, **Save** and **Sync branches now**.

| Field | Notes |
| --- | --- |
| Repo URL, access token | Fine-grained, read-only token (Contents + Metadata). Stored encrypted; never shown again. |
| Default Odoo version | Each branch can override it from the dropdown on the project page. |
| Edition | `enterprise` adds the Odoo Enterprise addons from `ENTERPRISE_REPO_URL` (branch = version). |
| Module name | Only for repos whose root *is* a module (has `__manifest__.py`): its technical name, e.g. `payroll_zm`. Blank = repo name. |
| Modules to install | Blank = every module in the repo. |
| Branch include / exclude | Regular expressions, e.g. include `^v18$`. |
| Extra addon repos | Other repos to put on the addons path (dependencies), as JSON. See the example below. |

Example: the payroll module depends on the bank module, which lives in another repo whose
branch names are not version numbers:

```json
[{"url": "https://github.com/ISWE-Solutions/bank_extension.git",
  "module_name": "bank_zm",
  "branches": {"18.0": "main", "19.0": "v19"}}]
```

A version without an entry in `branches` uses the branch named after the version (`18.0`).
Extra repos are fetched with the project's token.

Each preview's Odoo login is `admin` / `admin` (fresh database, sample data only).

## Roles

| Role | Can |
| --- | --- |
| Tester | View projects, open previews, wake, view logs |
| Developer | Tester rights, plus rebuild, stop, change branch version and settings |
| Admin | Everything: projects, Odoo versions, users, system settings |

## Production install

One Ubuntu 24.04 server (8 cores, 32 GB RAM, 300 GB SSD) with Docker.

1. **DNS:** A records for `preview.<domain>` and `*.preview.<domain>` pointing to the server.
2. **Firewall:** open only ports 22, 80 and 443.
3. **Configure:** `cp .env.example .env` and set:
   - `DOMAIN=preview.<domain>`
   - `TLS_ENABLED=true`
   - `ACME_EMAIL`, `DNS_PROVIDER` and `DNS_API_TOKEN`, used for the wildcard Let's Encrypt
     certificate (DNS-01 challenge). Check that `docker-compose.prod.yml` maps your
     provider's variable.
   - `DATA_DIR=/srv/previewer`
   - The secrets and `ADMIN_EMAIL`.
4. **Install:** run `scripts/install.sh`. It creates the folders, builds and starts the stack,
   runs the migrations, and asks for the first admin's password.

To start the stack by hand with HTTPS:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

**Backups:** back up only the app database, nightly. Preview databases are disposable.

```bash
docker compose exec -T postgres pg_dump -U previewer previewer | gzip > previewer-$(date +%F).sql.gz
```

## Operations

| Task | Command |
| --- | --- |
| Tests | `docker compose exec app pytest -q` |
| Worker logs | `docker compose logs -f worker` |
| Restart the worker after code changes | `docker compose restart worker` |
| Health | `curl http://preview.localhost/health` (reports the database, queue and worker heartbeat) |
| New migration | `docker compose run --rm app alembic revision --autogenerate -m "..."` |

Storage lives under `DATA_DIR`:

| Folder | Contents |
| --- | --- |
| `repos/` | Git mirrors |
| `builds/<id>/` | Exported code and `odoo.conf` |
| `filestore/<id>/` | Odoo data |
| `checkouts/` | Shared Enterprise exports |
| `logs/<id>.log` | Build and runtime logs |
| `logs/traefik/` | Traefik access log |

## Automation (worker scheduler)

Shown with their last result on **System** (admin); every value below is editable there.

| Task | When | What |
|---|---|---|
| poll | every 5 min | Sync every project (fallback for the GitHub webhook) |
| visits | 30 s | Read Traefik's access log; record each preview's last visit |
| sleep_idle | 1 min | Stop previews idle for 3 h (database kept); keep at most 25 running |
| disk_guard | 30 s | Hold new builds while less than 5 GB is free |
| cleanup_deleted / _stale / _builds | hourly / daily | Remove deleted branches after 24 h; stop branches with no commits or visits for 30 days (starred ones never), delete them after 60; drop failed logs after 14 days |
| prune_enterprise / prune_images / prune_db_templates | daily / weekly / 6 h | Remove unused Enterprise copies, this system's unused images, old ready-made databases |
| backup | daily | `pg_dump` of the app database to `DATA_DIR/backups/`, kept 14 days |

- **Webhook:** each project's Settings tab shows the payload URL and secret (events: push, create,
  delete). GitHub cannot reach `*.localhost`, so locally the poll does the work.
- **Login on previews:** previews ask the dashboard first (Traefik forward-auth). A logged-in
  user gets a separate *preview pass* cookie; the dashboard session itself never reaches a
  preview. Turn **Previews require a dashboard login** off per project for client demos.
- **Sleep and wake:** visiting a sleeping preview shows "Waking up…" and starts it (about 10 s).
- **Speed:** builds copy a ready-made database (base, plus Enterprise core) for their version,
  edition and demo setting, then install only the project's modules.
- **Notifications:** turn on **Post build results to GitHub** per project (commit statuses on pull
  requests); paste a Slack incoming-webhook URL on System for failure and "fixed" messages.
- **Why it failed:** failed builds show the cause (XML, view, data, Python, dependency, memory…)
  with the file and line linked on GitHub.

## Known limits

- Anonymised client database templates (Phase 3) are not built.
- HTTPS, the wildcard certificate and webhooks from GitHub need the real server and domain.
- Previews started before an upgrade keep their old settings (for example no login check) until
  they are rebuilt.
