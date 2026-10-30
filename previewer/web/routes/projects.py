import re
import shutil
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from ... import credentials
from ... import deps as project_deps
from ... import github, jobs
from ...config import get_settings
from ...db import get_db
from ...models import Branch, GithubCredential, Job, OdooVersion, Project, User, utcnow
from ...naming import preview_host, slugify
from ...security import decrypt, random_secret
from .. import accounts, selection
from ..deps import admin, can, developer, flash, paginate, paginate_list, render, tester
from ..views import branch_is_active, branch_status, project_counts, version_choices

router = APIRouter()

MODULE_RE = re.compile(r"^[a-z0-9_]+$")
FIELDS = ("name", "slug", "repo_url", "default_odoo_version", "edition", "module_name",
          "modules_to_install", "auto_build_regex", "branch_exclude_regex")
TABS = ("branches", "deps", "modules", "settings")
SELECTION = ("selected", "unselected", "all")


def get_project(s: Session, pid: int) -> Project:
    p = s.get(Project, pid)
    if not p:
        raise HTTPException(404)
    return p


# ---------- list ----------

LIST_TABS = {"projects": "repo", "combined": "combo", "plain": "plain"}


@router.get("/projects")
def projects_list(request: Request, q: str = "", page: int = 1, tab: str = "projects",
                  s: Session = Depends(get_db), user: User = Depends(tester)):
    tab = tab if tab in LIST_TABS else "projects"

    def of_kind(kind: str):
        stmt = select(Project).where(Project.kind == kind).order_by(Project.name)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(Project.name.ilike(like), Project.repo_url.ilike(like)))
        return stmt

    totals = {key: s.scalar(select(func.count()).select_from(of_kind(kind).subquery())) or 0
              for key, kind in LIST_TABS.items()}
    pg = paginate(s, of_kind(LIST_TABS[tab]), page, per_page=12)
    return render(request, "projects.html", {
        "page": pg, "q": q, "tab": tab, "totals": totals,
        "counts": {p.id: project_counts(p) for p in pg.items},
    })


# ---------- form ----------

def _parse_modules(value: str, errors: dict, field: str) -> str:
    mods = [m.strip() for m in value.replace("\n", ",").split(",") if m.strip()]
    bad = [m for m in mods if not MODULE_RE.match(m)]
    if bad:
        errors[field] = f"Invalid module name(s): {', '.join(bad)}"
    return ",".join(mods)


def validate(s: Session, form: dict, project: Project | None) -> tuple[dict, dict]:
    errors: dict = {}
    data = {k: (form.get(k) or "").strip() for k in FIELDS}
    if not data["name"]:
        errors["name"] = "Required"
    data["slug"] = slugify(data["slug"] or data["name"])[:40].strip("-")
    if not data["slug"]:
        errors["slug"] = "Required"
    else:
        clash = s.scalars(select(Project).where(Project.slug == data["slug"])).first()
        if clash and clash is not project:
            errors["slug"] = "Another project uses this slug"
    try:
        github.parse_repo(data["repo_url"])
    except github.GitHubError as e:
        errors["repo_url"] = str(e)
    v = s.get(OdooVersion, data["default_odoo_version"])
    if not v or not v.enabled:
        errors["default_odoo_version"] = "Choose an enabled Odoo version"
    if data["edition"] not in ("community", "enterprise"):
        errors["edition"] = "Choose community or enterprise"
    if data["module_name"] and not MODULE_RE.match(data["module_name"]):
        errors["module_name"] = "Lowercase letters, digits and _ only"
    data["modules_to_install"] = _parse_modules(data["modules_to_install"], errors, "modules_to_install")
    for key in ("auto_build_regex", "branch_exclude_regex"):
        try:
            re.compile(data[key])
        except re.error as e:
            errors[key] = f"Invalid regex: {e}"
    data["install_demo_data"] = form.get("install_demo_data") in ("on", "true", "1")
    data["credential_id"], err = accounts.parse_choice(s, form.get("credential_id"))
    if err:
        errors["credential_id"] = err
    if not errors and data["edition"] == "community":
        reason = community_blocker(s, data, project)
        if reason:
            errors["edition"] = reason
    return data, errors


def community_blocker(s: Session, data: dict, project: Project | None) -> str | None:
    """Reason Community cannot work for this project (its modules need Enterprise-only
    modules), checked on GitHub; None when fine or when it cannot be determined."""
    token = accounts.token_for_choice(s, str(data["credential_id"] or ""), credentials.default(s))
    probe = SimpleNamespace(repo_url=data["repo_url"],
                            root_module_name=data["module_name"] or
                            data["repo_url"].rstrip("/").removesuffix(".git").rsplit("/", 1)[-1])
    version = data["default_odoo_version"]
    if project:
        refs = [b.name for b in project.branches if b.enabled and not b.deleted_at][:3]
        dep_refs = [(d, d.branch_for(version)) for d in project.dependencies]
    else:
        refs, dep_refs = [], []
    try:
        refs = refs or [github.test_connection(data["repo_url"], token)["default_branch"]]
    except github.GitHubError:
        return None
    for ref in refs:
        needs = project_deps.enterprise_needs(s, probe, ref, version, dep_refs, token)
        if needs:
            return f"Branch {ref}: {project_deps.describe_needs(needs)}"
    return None


def account_ctx(s: Session) -> dict:
    return {"accounts": accounts.all_accounts(s), "default_account_label": accounts.default_choice_label(s)}


def _form_ctx(s: Session, project: Project | None, values: dict, errors: dict) -> dict:
    return {
        "project": project, "values": values, "errors": errors,
        "versions": version_choices(s, values.get("default_odoo_version") or None),
        **account_ctx(s),
    }


def _values_from(project: Project) -> dict:
    vals = {k: getattr(project, k) or "" for k in FIELDS}
    vals["install_demo_data"] = project.install_demo_data
    vals["credential_id"] = project.credential_id or ""
    return vals


@router.get("/projects/new")
def new_project(request: Request, s: Session = Depends(get_db), user: User = Depends(admin)):
    versions = version_choices(s)
    values = {"edition": "community", "default_odoo_version": versions[-1] if versions else ""}
    return render(request, "project_form.html", _form_ctx(s, None, values, {}))


@router.post("/projects/new")
async def create_project(request: Request, s: Session = Depends(get_db), user: User = Depends(admin)):
    form = dict(await request.form())
    data, errors = validate(s, form, None)
    if errors:
        return render(request, "project_form.html", _form_ctx(s, None, {**form, **data}, errors),
                      status_code=400)
    p = Project(**data, webhook_secret=random_secret(32))
    s.add(p)
    s.flush()
    jobs.enqueue(s, "sync", {"project_id": p.id})
    s.commit()
    flash(request, f"Project {p.name} created and its branches are being listed. Next: add any "
                   "dependency repos, then pick the branches to build on the Branches tab.", "success")
    return RedirectResponse(f"/projects/{p.id}?tab=deps", status_code=303)


@router.get("/projects/{pid}/edit")
def edit_project(pid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(admin)):
    p = get_project(s, pid)
    return RedirectResponse(f"/projects/{p.id}?tab=settings", status_code=303)


def rehost_branches(s: Session, project: Project) -> list[Branch]:
    """Recompute preview hosts after a slug change. Returns branches whose host changed."""
    domain = get_settings().domain
    changed = []
    for b in project.branches:
        new_host = preview_host(project.slug, b.slug, domain, combo=project.is_combo)
        if new_host == b.host:
            continue
        clash = s.scalars(select(Branch).where(Branch.host == new_host, Branch.id != b.id)).first()
        if clash:
            b.host, b.host_conflict = None, f"Host {new_host} is already used by {clash.project.name} / {clash.name}"
        else:
            b.host, b.host_conflict = new_host, None
        changed.append(b)
    return changed


@router.post("/projects/{pid}/edit")
async def update_project(pid: int, request: Request, s: Session = Depends(get_db),
                         user: User = Depends(admin)):
    p = get_project(s, pid)
    form = dict(await request.form())
    data, errors = validate(s, form, p)
    if errors:
        ctx = detail_ctx(request, s, p, "settings", form_values={**form, **data}, form_errors=errors)
        return render(request, "project_detail.html", ctx, status_code=400)
    old_slug = p.slug
    for k, v in data.items():
        setattr(p, k, v)
    s.flush()
    if p.slug != old_slug:
        rebuilt = 0
        for b in rehost_branches(s, p):
            s.flush()
            if b.host and b.enabled and b.live_build and b.last_commit_sha:
                jobs.queue_build(s, b)
                rebuilt += 1
        if rebuilt:
            flash(request, f"Slug changed: {rebuilt} preview(s) rebuilding on their new address.", "info")
    s.commit()
    flash(request, "Project saved.", "success")
    return RedirectResponse(f"/projects/{p.id}?tab=settings", status_code=303)


@router.post("/projects/test-connection")
async def test_connection(request: Request, s: Session = Depends(get_db), user: User = Depends(admin)):
    form = await request.form()
    url = (form.get("repo_url") or "").strip()
    cid, _ = accounts.parse_choice(s, form.get("credential_id"))
    cred = s.get(GithubCredential, cid) if cid else credentials.default(s)
    token = decrypt(cred.token_enc) if cred else ""
    who = escape(accounts.account_name(cred))
    try:
        info = github.test_connection(url, token)
        n = len(github.list_branches(url, token))
    except github.GitHubError as e:
        return HTMLResponse(f'<p class="text-sm text-red-700">✗ With {who}: {escape(str(e))}</p>')
    except Exception as e:  # noqa: BLE001  (network errors)
        return HTMLResponse(f'<p class="text-sm text-red-700">✗ Could not reach GitHub: {escape(e.__class__.__name__)}</p>')
    return HTMLResponse(
        f'<p class="text-sm text-green-700">✓ Connected to {escape(info["full_name"])} with {who} '
        f'({"private" if info["private"] else "public"}, default branch '
        f'{escape(info["default_branch"])}, {n} branches)</p>'
    )


@router.post("/projects/{pid}/delete")
async def delete_project(pid: int, request: Request, s: Session = Depends(get_db),
                         user: User = Depends(admin)):
    p = get_project(s, pid)
    form = await request.form()
    back = RedirectResponse(f"/combos/{p.id}?tab=settings" if p.is_combo else
                            f"/projects/{p.id}?tab=settings", status_code=303)
    if (form.get("confirm_slug") or "").strip() != p.slug:
        flash(request, f"Type {p.slug} to confirm deleting the project.", "error")
        return back
    building = [b.name for b in p.branches if any(x.status == "building" for x in b.builds)]
    if building:
        flash(request, f"Cannot delete {p.name} while {', '.join(building)} is building. "
                       "Try again when the build finishes.", "error")
        return back
    branch_ids = [b.id for b in p.branches]
    if branch_ids:
        s.execute(update(Job).where(Job.branch_id.in_(branch_ids), Job.status == "queued")
                  .values(status="cancelled", finished_at=utcnow()))
    previews = 0
    for b in p.branches:
        for build in b.builds:
            # Every build, so leftover logs and directories go too (teardown is idempotent).
            jobs.enqueue(s, "delete", {"build_id": build.id, "container_id": build.container_id,
                                       "db_name": build.db_name, "purge_log": True})
            previews += build.status in ("running", "sleeping", "stopped")
    name = p.name
    mirror = get_settings().repos_dir / f"project-{p.id}.git"
    s.delete(p)
    s.commit()
    shutil.rmtree(mirror, ignore_errors=True)
    flash(request, f"Project {name} deleted; {previews} preview(s) and all its builds are being "
                   "removed.", "success")
    return RedirectResponse("/projects", status_code=303)


# ---------- detail ----------

def default_selection(p: Project) -> str:
    return "selected" if any(b.enabled and not b.deleted_at for b in p.branches) else "all"


def branches_ctx(s: Session, p: Project, q: str, status: str, page: int, sel: str = "") -> dict:
    sel = sel if sel in SELECTION else default_selection(p)
    rows = [b for b in p.branches if not b.deleted_at or status == "deleted"]
    if sel == "selected":
        rows = [b for b in rows if b.enabled]
    elif sel == "unselected":
        rows = [b for b in rows if not b.enabled]
    if q:
        rows = [b for b in rows if q.lower() in b.name.lower()]
    if status == "deleted":
        rows = [b for b in rows if b.deleted_at]
    elif status:
        rows = [b for b in rows if branch_status(b) == status]
    rows.sort(key=lambda b: (not b.enabled, not b.sticky, b.name))
    pending_syncs = s.scalars(select(Job).where(Job.type == "sync", Job.status.in_(("queued", "running")))).all()
    sync_pending = any((j.payload or {}).get("project_id") == p.id for j in pending_syncs)
    listed = [b for b in p.branches if not b.deleted_at]
    return {
        "project": p, "page": paginate_list(rows, page), "q": q, "status": status, "sel": sel,
        "versions": version_choices(s, p.default_odoo_version),
        "sync_pending": sync_pending,
        "polling": sync_pending or any(branch_is_active(b) for b in p.branches),
        "n_selected": sum(1 for b in listed if b.enabled), "n_listed": len(listed),
    }


def deps_ctx(s: Session, p: Project, dq: str, page: int) -> dict:
    rows = list(p.dependencies)
    if dq:
        needle = dq.lower()
        rows = [d for d in rows if needle in d.repo_url.lower() or needle in d.root_module_name.lower()]
    versions = version_choices(s)
    return {
        "deps_page": paginate_list(rows, page), "dq": dq, "dep_versions": versions,
        "enterprise": ({v: accounts.enterprise_status(s, v) for v in versions}
                       if p.edition == "enterprise" else None),
        "dep_account": {d.id: accounts.dependency_account_desc(d) for d in rows},
    }


def detail_ctx(request: Request, s: Session, p: Project, tab: str, *, q: str = "", status: str = "",
               page: int = 1, sel: str = "", dq: str = "", mq: str = "",
               form_values: dict | None = None, form_errors: dict | None = None) -> dict:
    tab = tab if tab in TABS else "branches"
    user = getattr(request.state, "user", None)
    if tab == "settings" and not can(user, "admin"):
        tab = "branches"
    ctx = {"project": p, "tab": tab, "n_deps": len(p.dependencies), "n_sets": len(p.module_sets)}
    if tab == "branches":
        ctx.update(branches_ctx(s, p, q, status, page, sel))
    elif tab == "deps":
        ctx.update(deps_ctx(s, p, dq, page))
    elif tab == "modules":
        from .modules import modules_ctx
        ctx.update(modules_ctx(s, p, mq, page))
    else:
        values = form_values or _values_from(p)
        ctx.update({"values": values, "errors": form_errors or {},
                    "versions": version_choices(s, values.get("default_odoo_version") or None),
                    **account_ctx(s)})
    return ctx


@router.get("/projects/{pid}")
def project_detail(pid: int, request: Request, tab: str = "branches", q: str = "", status: str = "",
                   page: int = 1, sel: str = "", dq: str = "", mq: str = "",
                   s: Session = Depends(get_db), user: User = Depends(tester)):
    p = get_project(s, pid)
    if p.is_combo:
        return RedirectResponse(f"/combos/{p.id}", status_code=303)
    return render(request, "project_detail.html",
                  detail_ctx(request, s, p, tab, q=q, status=status, page=page, sel=sel, dq=dq,
                             mq=mq))


@router.get("/projects/{pid}/branches")
def branches_partial(pid: int, request: Request, q: str = "", status: str = "", page: int = 1,
                     sel: str = "", s: Session = Depends(get_db), user: User = Depends(tester)):
    p = get_project(s, pid)
    return render(request, "_branches_table.html", branches_ctx(s, p, q, status, page, sel))


@router.post("/projects/{pid}/branches/bulk")
async def bulk_select(pid: int, request: Request, s: Session = Depends(get_db),
                      user: User = Depends(developer)):
    p = get_project(s, pid)
    form = await request.form()
    ids = {int(x) for x in form.getlist("ids") if str(x).isdigit()}
    chosen = [b for b in p.branches if b.id in ids and not b.deleted_at]
    action = form.get("action")
    if not chosen:
        flash(request, "Tick at least one branch first.", "error")
    elif action == "unselect":
        for b in chosen:
            if b.enabled:
                selection.unselect(s, b)
        flash(request, f"{len(chosen)} branch(es) unselected.", "info")
    elif action in ("pause", "resume"):
        for b in chosen:
            b.auto_rebuild = action == "resume"
        flash(request, f"Auto-rebuild {'resumed' if action == 'resume' else 'paused'} for "
                       f"{', '.join(b.name for b in chosen)}.", "info")
    else:
        results = [(b, selection.select(request, s, b, quiet=True)) for b in chosen]
        queued = [b.name for b, r in results if r == "queued"]
        blocked = [b.name for b, r in results if r == "blocked"]
        skipped = [b.name for b, r in results if r == "skipped"]
        if queued:
            flash(request, f"Building {', '.join(queued)}.", "success")
        if blocked:
            flash(request, f"Selected but blocked by missing dependencies: {', '.join(blocked)}.", "error")
        if skipped:
            flash(request, f"Selected, not built (no commit or preview host yet): {', '.join(skipped)}.", "info")
    s.commit()
    qp = request.query_params
    if request.headers.get("hx-request"):
        return render(request, "_branches_table.html",
                      branches_ctx(s, p, qp.get("q", ""), qp.get("status", ""),
                                   int(qp.get("page", 1) or 1), qp.get("sel", "")))
    return RedirectResponse(f"/projects/{p.id}", status_code=303)


@router.post("/projects/{pid}/sync")
def sync_project(pid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(developer)):
    p = get_project(s, pid)
    jobs.enqueue(s, "sync", {"project_id": p.id})
    s.commit()
    flash(request, "Branch sync queued.", "info")
    if request.headers.get("hx-request"):
        return HTMLResponse("", headers={"HX-Redirect": f"/projects/{p.id}"})
    return RedirectResponse(f"/projects/{p.id}", status_code=303)
