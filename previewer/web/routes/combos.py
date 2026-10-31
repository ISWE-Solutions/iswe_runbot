"""Combined previews: several repos, each at a chosen branch, installed together in one Odoo.

A combo is a Project(kind="combo") with no repo of its own. Its parts are its
ProjectDependency rows; its single Branch (models.COMBO_BRANCH) pins each part's branch in
dependency_branches. The worker's `sync` of a combo resolves the parts' heads into one
combined commit and builds it.
"""
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import credentials, github
from ...config import get_settings
from ...db import get_db
from ...models import COMBO_BRANCH, Branch, Build, OdooVersion, Project, ProjectDependency, User
from ...naming import preview_host, slugify
from ...security import random_secret
from .. import accounts, selection
from . import plain as plain_routes
from ..deps import current_user, flash, paginate, render
from ..views import version_choices
from .dependencies import branch_names
from .projects import _parse_modules, rehost_branches

router = APIRouter()

MODULE_RE = re.compile(r"^[a-z0-9_]+$")
PART_RE = re.compile(r"^parts-(\d+)-source$")
TABS = ("preview", "builds", "plan", "parts", "modules", "settings")
PLAIN_TABS = ("preview", "builds", "plan", "modules", "settings")
SETTING_FIELDS = ("name", "slug", "default_odoo_version", "edition", "modules_to_install")


# ---------- lookups ----------

def get_combo(s: Session, pid: int) -> Project:
    p = s.get(Project, pid)
    if not p or not p.is_combo:
        raise HTTPException(404)
    return p


def combo_branch(p: Project) -> Branch:
    b = next((b for b in p.branches if b.name == COMBO_BRANCH), None)
    if b is None:
        raise HTTPException(404)
    return b


def repo_projects(s: Session) -> list[Project]:
    return list(s.scalars(select(Project).where(Project.kind == "repo").order_by(Project.name)))


def _source_for(repo_url: str, projects: list[Project]) -> str:
    """'project:<id>' when the repo is a registered project's, else 'other'."""
    try:
        url = github.clone_url(repo_url)
    except github.GitHubError:
        return "other"
    for p in projects:
        try:
            if github.clone_url(p.repo_url) == url:
                return f"project:{p.id}"
        except github.GitHubError:
            continue
    return "other"


# ---------- parts editor ----------

def blank_part(i: int) -> dict:
    return {"i": i, "source": "", "repo_url": "", "module_name": "", "credential_id": "",
            "branch": "", "dep_id": ""}


def parts_from_combo(p: Project, projects: list[Project]) -> list[dict]:
    b = combo_branch(p)
    pins = b.dependency_branches or {}
    return [{"i": i, "source": _source_for(d.repo_url, projects), "repo_url": d.repo_url,
             "module_name": d.module_name, "credential_id": str(d.credential_id or ""),
             "branch": pins.get(str(d.id), ""), "dep_id": str(d.id)}
            for i, d in enumerate(p.dependencies)]


def _row_from_form(form, i: int) -> dict:
    get = lambda k: (form.get(f"parts-{i}-{k}") or "").strip()  # noqa: E731
    return {"i": i, "source": get("source"), "repo_url": get("repo_url"),
            "module_name": get("module_name"), "credential_id": get("credential_id"),
            "branch": get("branch"), "dep_id": get("dep_id")}


def _prefill_from_project(s: Session, part: dict) -> Project | None:
    """A part picked from a registered project takes its repo, module name and account."""
    src = part["source"]
    if not src.startswith("project:") or not src[8:].isdigit():
        return None
    p = s.get(Project, int(src[8:]))
    if p is None or p.is_combo:
        return None
    part["repo_url"], part["module_name"] = p.repo_url, p.module_name
    part["credential_id"] = str(p.credential_id or "")
    return p


def parse_parts(s: Session, form) -> tuple[list[dict], dict]:
    idx = sorted(int(m[1]) for k in form.keys() if (m := PART_RE.match(k)))
    parts, errors = [], {}
    seen: set[str] = set()
    for i in idx:
        part = _row_from_form(form, i)
        if not part["source"] and not part["repo_url"]:
            continue  # untouched empty row
        if part["source"].startswith("project:") and not part["repo_url"]:
            _prefill_from_project(s, part)
        err = []
        try:
            url = github.clone_url(part["repo_url"])
            if url in seen:
                err.append("this repository is already a part")
            seen.add(url)
        except github.GitHubError as e:
            err.append(str(e))
        if part["module_name"] and not MODULE_RE.match(part["module_name"]):
            err.append("module name: lowercase letters, digits and _ only")
        if not part["branch"]:
            err.append("choose a branch")
        _, cerr = accounts.parse_choice(s, part["credential_id"])
        if cerr:
            err.append(cerr)
        if err:
            errors[f"part_{i}"] = "; ".join(err)
        parts.append(part)
    if not parts:
        errors["parts"] = "Add at least one repository"
    return parts, errors


def _part_name(part: dict) -> str:
    if part["module_name"]:
        return part["module_name"]
    return part["repo_url"].rstrip("/").removesuffix(".git").rsplit("/", 1)[-1] or "part"


def part_ctx(s: Session, part: dict, *, branches=None, load_error=None, error=None) -> dict:
    return {"part": part, "projects": repo_projects(s), "accounts": accounts.all_accounts(s),
            "default_label": accounts.default_choice_label(s), "branches": branches,
            "load_error": load_error, "error": error}


def _load_branches(s: Session, part: dict) -> tuple[list[str] | None, str | None]:
    if not part["repo_url"]:
        return None, None
    token = accounts.token_for_choice(s, part["credential_id"], credentials.default(s))
    try:
        return branch_names(part["repo_url"], token), None
    except github.GitHubError as e:
        return None, str(e)
    except Exception as e:  # noqa: BLE001
        return None, f"Could not reach GitHub: {e.__class__.__name__}"


@router.get("/combos/part-row")
def new_part_row(request: Request, i: int = 0, s: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    return render(request, "_combo_part_row.html", part_ctx(s, blank_part(i)))


@router.post("/combos/part-row")
async def refresh_part_row(request: Request, s: Session = Depends(get_db),
                           user: User = Depends(current_user)):
    """HTMX: re-render one part row; picking a project prefills it; loads its branches."""
    form = await request.form()
    i = int(form.get("row") or 0)
    part = _row_from_form(form, i)
    if form.get("action") == "source":
        if part["source"].startswith("project:"):
            _prefill_from_project(s, part)
        elif part["source"] == "other":
            part.update(repo_url="", module_name="", credential_id="")
        part["branch"] = ""
    branches, error = _load_branches(s, part)
    return render(request, "_combo_part_row.html", part_ctx(s, part, branches=branches,
                                                              load_error=error))


def save_parts(s: Session, p: Project, b: Branch, parts: list[dict]) -> None:
    """Make the combo's dependency rows and pins match the submitted parts."""
    keep: dict[int, ProjectDependency] = {d.id: d for d in p.dependencies}
    pins: dict[str, str] = {}
    used: set[int] = set()
    for pos, part in enumerate(parts):
        did = int(part["dep_id"]) if part["dep_id"].isdigit() else None
        d = keep.get(did) if did else None
        if d is None:
            d = ProjectDependency(project_id=p.id)
            s.add(d)
        d.position, d.repo_url, d.module_name = pos, part["repo_url"], part["module_name"]
        d.branch_map = {}
        d.credential_id, _ = accounts.parse_choice(s, part["credential_id"])
        s.flush()
        used.add(d.id)
        pins[str(d.id)] = part["branch"]
    for did, d in keep.items():
        if did not in used:
            s.delete(d)
    b.dependency_branches = pins
    s.flush()
    s.expire(p, ["dependencies"])


# ---------- settings ----------

def validate_settings(s: Session, form, combo: Project | None) -> tuple[dict, dict]:
    errors: dict = {}
    data = {k: (form.get(k) or "").strip() for k in SETTING_FIELDS}
    if not data["name"]:
        errors["name"] = "Required"
    data["slug"] = slugify(data["slug"] or data["name"])[:40].strip("-")
    if not data["slug"]:
        errors["slug"] = "Required"
    else:
        clash = s.scalars(select(Project).where(Project.slug == data["slug"])).first()
        own = combo_branch(combo) if combo else None
        host = preview_host(data["slug"], COMBO_BRANCH, get_settings().domain, combo=True)
        taken = s.scalars(select(Branch).where(Branch.host == host)).first()
        if clash and clash is not combo:
            errors["slug"] = "Another project or combined preview uses this slug"
        elif taken and taken is not own:
            errors["slug"] = f"The address {host} is already used by {taken.project.name} / {taken.name}"
    v = s.get(OdooVersion, data["default_odoo_version"])
    if not v or not v.enabled:
        errors["default_odoo_version"] = "Choose an enabled Odoo version"
    if data["edition"] not in ("community", "enterprise"):
        errors["edition"] = "Choose community or enterprise"
    data["modules_to_install"] = _parse_modules(data["modules_to_install"], errors, "modules_to_install")
    data["install_demo_data"] = form.get("install_demo_data") in ("on", "true", "1")
    return data, errors


def _settings_values(p: Project) -> dict:
    vals = {k: getattr(p, k) or "" for k in SETTING_FIELDS}
    vals["install_demo_data"] = p.install_demo_data
    return vals


def form_ctx(s: Session, values: dict, parts: list[dict], errors: dict, combo: Project | None = None) -> dict:
    return {"values": values, "parts": [part_ctx(s, part, error=errors.get(f"part_{part['i']}"))
                                        for part in parts],
            "errors": errors, "combo": combo, "next_i": max((x["i"] for x in parts), default=-1) + 1,
            "versions": version_choices(s, values.get("default_odoo_version") or None)}


def _outcome_flash(request: Request, p: Project, b: Branch, outcome: str, verb: str) -> None:
    if outcome == "queued":
        flash(request, f"{p.name} {verb}. Its parts are being resolved and built.", "success")
    elif outcome == "blocked":
        flash(request, f"{p.name} {verb} but not built: " + "; ".join(
            r.message for r in selection.blocked(b)) + ". Fix the parts, or use Build anyway.", "error")
    else:
        flash(request, f"{p.name} {verb}.", "info")


@router.get("/combos/new")
def new_combo(request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    versions = version_choices(s)
    values = {"edition": "community", "default_odoo_version": versions[-1] if versions else ""}
    return render(request, "combo_form.html",
                  form_ctx(s, values, [blank_part(0), blank_part(1)], {}))


@router.post("/combos/new")
async def create_combo(request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    data, errors = validate_settings(s, form, None)
    parts, perrors = parse_parts(s, form)
    errors.update(perrors)
    if errors:
        return render(request, "combo_form.html", form_ctx(s, {**dict(form), **data}, parts or [blank_part(0)],
                                                           errors), status_code=400)
    p = Project(kind="combo", repo_url="", webhook_secret=random_secret(32), **data)
    s.add(p)
    s.flush()
    b = Branch(project_id=p.id, name=COMBO_BRANCH, slug=COMBO_BRANCH, enabled=True,
               host=preview_host(p.slug, COMBO_BRANCH, get_settings().domain, combo=True))
    s.add(b)
    s.flush()
    save_parts(s, p, b, parts)
    outcome = selection.queue_combo(request, s, b)
    s.commit()
    _outcome_flash(request, p, b, outcome, "created")
    return RedirectResponse(f"/combos/{p.id}", status_code=303)


# ---------- detail ----------

def preview_ctx(s: Session, p: Project, b: Branch, page: int) -> dict:
    builds = paginate(s, select(Build).where(Build.branch_id == b.id).order_by(Build.id.desc()), page)
    latest = next((x for x in b.builds if x.dependency_refs), None)
    built = {r.get("name"): r for r in (latest.dependency_refs if latest else [])}
    return {"builds": builds, "built": built, "parts_list": selection.dep_summary(b)}


@router.get("/combos/{pid}")
def combo_detail(pid: int, request: Request, tab: str = "preview", page: int = 1, mq: str = "",
                 s: Session = Depends(get_db), user: User = Depends(current_user)):
    p = get_combo(s, pid)
    b = combo_branch(p)
    tab = tab if tab in (PLAIN_TABS if p.is_plain else TABS) else "preview"
    ctx = {"project": p, "branch": b, "tab": tab}
    if tab in ("preview", "builds", "plan"):
        ctx.update(preview_ctx(s, p, b, page))
    elif tab == "modules":
        from .modules import modules_ctx
        ctx.update(modules_ctx(s, p, mq, page))
    elif tab == "parts":
        ctx.update(form_ctx(s, _settings_values(p), parts_from_combo(p, repo_projects(s)) or [blank_part(0)],
                            {}, p))
    elif p.is_plain:
        ctx.update(plain_routes.form_ctx(s, _settings_values(p), {}))
    else:
        ctx.update(form_ctx(s, _settings_values(p), [], {}, p))
    return render(request, "combo_detail.html", ctx)


@router.post("/combos/{pid}/parts")
async def update_parts(pid: int, request: Request, s: Session = Depends(get_db),
                       user: User = Depends(current_user)):
    p = get_combo(s, pid)
    b = combo_branch(p)
    form = await request.form()
    parts, errors = parse_parts(s, form)
    if errors:
        ctx = {"project": p, "branch": b, "tab": "parts",
               **form_ctx(s, _settings_values(p), parts or [blank_part(0)], errors, p)}
        return render(request, "combo_detail.html", ctx, status_code=400)
    save_parts(s, p, b, parts)
    results = selection.run_check(b)
    if any(not r.ok for r in results):
        selection.mark_blocked(b, results)
        outcome = "blocked"
    else:
        selection.clear_blocked(b.id)
        selection.refresh_combo(s, p.id)
        outcome = "queued" if b.auto_rebuild else "paused"
    s.commit()
    if outcome == "paused":
        flash(request, f"Parts saved. Auto-rebuild is paused for {p.name}: press Rebuild to build them.", "info")
    else:
        _outcome_flash(request, p, b, outcome, "parts saved")
    return RedirectResponse(f"/combos/{p.id}", status_code=303)


def _plain_settings(s: Session, form, p: Project) -> tuple[dict, dict]:
    """A plain preview's settings come from the Try Odoo fields (radios, app grid)."""
    errors: dict = {}
    edition = (form.get("edition") or "").strip()
    modules = plain_routes.modules_from_form(form, edition, errors)
    fields = {**dict(form), "modules_to_install": modules,
              "install_demo_data": "on" if form.get("install_demo_data") == "with" else ""}
    data, more = validate_settings(s, fields, p)
    errors.update(more)
    if edition == "enterprise" and data["default_odoo_version"] and "default_odoo_version" not in errors:
        st = accounts.enterprise_status(s, data["default_odoo_version"])
        if st["ok"] is not True:
            errors["edition"] = (f"Enterprise is not available for Odoo {data['default_odoo_version']}: "
                                 f"{st['message']}")
    return data, errors


@router.post("/combos/{pid}/settings")
async def update_settings(pid: int, request: Request, s: Session = Depends(get_db),
                          user: User = Depends(current_user)):
    p = get_combo(s, pid)
    b = combo_branch(p)
    form = await request.form()
    if p.is_plain:
        data, errors = _plain_settings(s, form, p)
    else:
        data, errors = validate_settings(s, form, p)
    if errors:
        ctx = {"project": p, "branch": b, "tab": "settings"}
        if p.is_plain:
            ctx.update(plain_routes.form_ctx(s, {**dict(form), **data, "apps": form.getlist("apps"),
                                                  "other_modules": form.get("other_modules") or ""},
                                             errors))
        else:
            ctx.update(form_ctx(s, {**dict(form), **data}, [], errors, p))
        return render(request, "combo_detail.html", ctx, status_code=400)
    rebuild_keys = ("default_odoo_version", "edition", "modules_to_install", "install_demo_data")
    changed = any(getattr(p, k) != data[k] for k in rebuild_keys)
    if "module_set_id" in form:
        sid = int(form.get("module_set_id") or 0)
        new_set = next((m for m in p.module_sets if m.id == sid), None)
        changed = changed or new_set is not p.module_set
        p.module_set = new_set
    old_slug = p.slug
    for k, v in data.items():
        setattr(p, k, v)
    s.flush()
    if p.slug != old_slug:
        rehost_branches(s, p)
        s.flush()
        changed = changed or bool(b.live_build)
    outcome = selection.queue_combo(request, s, b) if changed else None
    s.commit()
    if outcome:
        _outcome_flash(request, p, b, outcome, "saved")
    else:
        flash(request, f"{p.name} saved.", "success")
    return RedirectResponse(f"/combos/{p.id}?tab=settings", status_code=303)


@router.post("/combos/{pid}/refresh")
def refresh(pid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    p = get_combo(s, pid)
    selection.refresh_combo(s, p.id)
    s.commit()
    flash(request, f"Refreshing {p.name}: the parts' latest commits are being checked.", "info")
    return RedirectResponse(f"/combos/{p.id}", status_code=303)


@router.post("/combos/{pid}/delete")
async def delete_combo(pid: int, request: Request, s: Session = Depends(get_db),
                       user: User = Depends(current_user)):
    from .projects import delete_project
    get_combo(s, pid)
    return await delete_project(pid, request, s, user)
