# Odoo Branch Previewer — conventions

Runbot-style previews: every branch of a registered GitHub repo runs as its own Odoo
container at `<project>-<branch>.<DOMAIN>`. Spec: `SPEC.md` (Phase 1 = core previews).

## Layout

```
previewer/
  config.py        Settings from .env (pydantic-settings). DATA_DIR layout helpers.
  db.py            engine/session; db.configure(url) rebinds (tests)
  models.py        all tables; change together with a new Alembic migration
  naming.py        slug / host label / db name rules (63-char limit + 6-char hash)
  security.py      bcrypt, Fernet encrypt/decrypt (GitHub tokens), random secrets
  jobs.py          Postgres queue: enqueue, queue_build, claim (SKIP LOCKED), finish
  appsettings.py   runtime settings in the `settings` table, defaults from .env
  github.py        test_connection, list_branches, fill_commit_details, branch_exists
  deps.py          resolve(branch) -> dependency repos + branches; check() verifies on GitHub
  credentials.py   which Git credential pulls what: for_project, for_dependency, enterprise_source
  modules.py       install planning (pure): plan(), top_level(), explain(), odoo_dependency_errors()
  diagnose.py      why a build failed (pure): diagnose(log, mounts, signals) -> [Finding], headline()
  gitmirror.py     bare mirrors under DATA_DIR/repos, export commits    (worker only)
  cli.py           python -m previewer.cli seed | create-user
  web/             FastAPI app (main.py), routes, Jinja + HTMX templates, Tailwind
  worker/          python -m previewer.worker: job loop, build pipeline, Docker ops
migrations/        Alembic (runs on app start via scripts/start-app.sh)
tests/             pytest, run inside the app container
```

## Commands

```bash
docker compose up -d --build                 # whole stack (dev override auto-loaded)
docker compose exec app pytest -q            # tests (own throwaway DB, see tests/conftest.py)
docker compose exec -e TEST_DATABASE=previewer_test_x app pytest   # parallel test runs
docker compose restart worker                # worker has no auto-reload
docker compose run --rm app alembic revision --autogenerate -m "..."
docker compose exec app python -m previewer.cli create-user --email you@x --role admin
```

Dashboard: http://preview.localhost (browsers and curl resolve `*.localhost` to 127.0.0.1).

## Rules

- Only the worker talks to Docker. The web app enqueues jobs and reads the DB and log files.
- Job payloads: `build {build_id}` (always via `jobs.queue_build`), `sync {project_id}`,
  `stop|start|wake {build_id}`, `delete {build_id, container_id, db_name, purge_log?}` (snapshot,
  since the build row may be gone; purge_log also removes the log), `pull {version}`.
- DATA_DIR is mounted at the same absolute path in app, worker and host, so paths the
  worker passes to `docker run` resolve on the host. It may contain spaces: never build
  shell strings from it; inside preview containers use fixed paths (`/mnt/src`, ...).
- Never log or render GitHub tokens. Decrypt only at the point of use, via `credentials`.
- Git credentials live in `github_credentials` (one `is_default`). Project account = its
  credential or the default; dependency = its credential or the project's; Enterprise per
  Odoo version = `odoo_versions.enterprise_repo_url/branch/credential` falling back to
  ENTERPRISE_REPO_URL / the version name / the default account. Use `Auth.label` in logs.
- Repos whose root is a module (has `__manifest__.py`) are exported to `src/<module_name>`;
  `module_name` defaults to the repo name (payroll_extension -> `payroll_zm`, bank -> `bank_zm`).
- Dependencies are `project_dependencies` rows. Branch for a build = branch override
  (`Branch.dependency_branches[str(dep.id)]`) > `dep.branch_map[version]` > branch named after the
  version. Always go through `deps.resolve` so the setup screens and the worker agree.
- Branch selection: sync lists every branch not matching `branch_exclude_regex`; only
  `Branch.enabled` ("selected") branches are built. New branches start selected only if they
  match `auto_build_regex`.
- `Branch.auto_rebuild` off: new commits are recorded but never build the branch (sync,
  and Phase 2 webhooks/polling); manual Rebuild still works. Deleting all of a branch's
  builds does not trigger a rebuild by itself.
- Combined previews: `Project.kind == "combo"`, no repo of its own (`repo_url` ""). Its parts are
  its `project_dependencies`; its single Branch row is named `models.COMBO_BRANCH` and pins each
  part via `dependency_branches`. Host = `<slug>.<domain>` (`preview_host(..., combo=True)`).
  `last_commit_sha` / `Build.commit_sha` = `deps.combo_commit([(repo_url, head sha), ...])`.
  The `sync` job of a combo refreshes the parts' heads instead of listing GitHub branches.
- Modules to install, in order: branch modules_override > branch module_set > project module_set >
  project modules_to_install > auto (every module of the project repo; for combos, of all parts).
  `ModuleSet` = named collection on a project. The worker computes `modules.plan()` from the
  exported files before Odoo runs and saves `Build.install_plan`; missing dependencies fail the
  build with `modules.explain()`. The web side uses `deps.module_plan()` (same plan from GitHub).
- Failure diagnosis: on every failed build the worker runs `diagnose.diagnose()` over the build
  log (with in-container mount -> repo/ref `Source`s for GitHub links, plus OOM/timeout/health
  `Signals`), saves `Build.diagnosis` and puts `diagnose.headline()` first in error_summary.
  Patterns come from real Odoo 18/19 logs in tests/fixtures/odoo_logs; add a fixture for new cases.
- Periodic work runs on the worker's scheduler thread (`worker/scheduler.py`): task modules
  `worker/automation.py` (poll, sleep idle, visit tracking, running limit, disk guard, stats) and
  `worker/maintenance.py` (cleanup, prune, backups) each export `TASKS = [Task(name, every, run)]`.
  Last runs live in `scheduler_state`; settings come from `appsettings` at every tick.
- Build events: the pipeline calls `worker/notify.build_event(build_id, "building"|"running"|"failed")`
  (GitHub commit status when `Project.post_commit_status`, Slack when `slack_webhook_url_enc` is set).
  It must never raise.
- Previews: every preview router has the `previewer-auth@docker` forward-auth middleware (defined on
  the app service). `/auth/forward` decides per request from the host: allowed when the project has
  `preview_auth_required` off, or the request has a valid `previewer_pass` cookie (signed, on
  .DOMAIN, 12 h, issued by `/auth/preview-pass` to logged-in users). The dashboard session cookie
  stays host-only so code running in a preview can never use it on the dashboard.
- Sleep: a `sleeping` build has its container stopped but keeps database + filestore; a visit to its
  host reaches the dashboard catch-all, which shows "Waking up" and enqueues one `wake` job.
  Visits are read from Traefik's JSON access log into `Build.last_accessed_at`.
- DB templates (`db_templates`): builds copy a ready database (base + Enterprise core for that
  version/edition/demo/image/Enterprise commit) with its filestore instead of installing from
  scratch, when the `use_db_templates` setting is 1.
- Build status values: queued, building, running, sleeping, failed, stopped, removed.
- Python 3.12, SQLAlchemy 2.0 style (`select()`), type hints, small modules, no comments
  that restate the code.
