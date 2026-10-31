"""Modules tab: available modules, module sets (named collections), install plan previews."""
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import Branch, ModuleSet, Project, User
from .. import module_view as mv
from ..deps import current_user, flash, paginate_list, render

router = APIRouter()

MODULE_RE = re.compile(r"^[a-z0-9_]+$")


def get_owner(s: Session, pid: int) -> Project:
    p = s.get(Project, pid)
    if not p:
        raise HTTPException(404)
    return p


def tab_url(p: Project) -> str:
    return f"/combos/{p.id}?tab=modules" if p.is_combo else f"/projects/{p.id}?tab=modules"


def get_set(p: Project, sid: int) -> ModuleSet:
    ms = next((m for m in p.module_sets if m.id == sid), None)
    if ms is None:
        raise HTTPException(404)
    return ms


def pick_branch(p: Project, branch_id: int | None) -> Branch | None:
    choices = mv.branch_choices(p)
    if branch_id:
        return next((b for b in choices if b.id == branch_id), None)
    return choices[0] if choices else None


def modules_ctx(s: Session, p: Project, mq: str, page: int) -> dict:
    """Context for the Modules tab of a project or combined preview."""
    sets = list(p.module_sets)
    if mq:
        needle = mq.lower()
        sets = [m for m in sets if needle in m.name.lower() or needle in m.modules.lower()]
    return {
        "sets_page": paginate_list(sets, page), "mq": mq,
        "branch_choices": mv.branch_choices(p), "default_branch": pick_branch(p, None),
        "project_default": mv.project_default_desc(p),
        "set_users": {m.id: mv.set_users(s, m) for m in p.module_sets},
    }


@router.get("/projects/{pid}/modules/available")
def available_modules(pid: int, request: Request, branch_id: int | None = None,
                      s: Session = Depends(get_db), user: User = Depends(current_user)):
    p = get_owner(s, pid)
    b = pick_branch(p, branch_id)
    av = mv.available(b) if b else None
    return render(request, "_modules_available.html", {"project": p, "branch": b, "av": av})


@router.get("/projects/{pid}/modules/checkboxes")
def module_checkboxes(pid: int, request: Request, branch_id: int | None = None, chosen: str = "",
                      s: Session = Depends(get_db), user: User = Depends(current_user)):
    """Module picker for the module set form; keeps what is already chosen."""
    p = get_owner(s, pid)
    b = pick_branch(p, branch_id)
    params = request.query_params
    picked = mv.parse_module_list(params.getlist("modules"),
                                  ",".join(filter(None, [chosen, params.get("modules_text", "")])))
    av = mv.available(b) if b else None
    return render(request, "_module_checkboxes.html", {"project": p, "branch": b, "av": av,
                                                        "chosen": picked})


def _plan_response(request: Request, p: Project, b: Branch | None, requested: list[str]):
    if b is None:
        return render(request, "_plan_preview.html", {"plan": None, "error": "No branch to plan for."})
    av = mv.available(b)
    if av.error:
        return render(request, "_plan_preview.html", {"plan": None, "error": av.error})
    plan, why = mv.plan_for(av, requested)
    return render(request, "_plan_preview.html", {
        "plan": plan, "why": why, "groups": mv.grouped_install(plan), "branch": b,
        "requested_auto": not requested,
    })


@router.post("/projects/{pid}/modules/plan")
async def plan_preview(pid: int, request: Request, s: Session = Depends(get_db),
                       user: User = Depends(current_user)):
    p = get_owner(s, pid)
    form = await request.form()
    b = pick_branch(p, int(form.get("branch_id") or 0) or None)
    requested = mv.parse_module_list(form.getlist("modules"), form.get("modules_text") or "")
    return _plan_response(request, p, b, requested)


@router.post("/branches/{bid}/plan")
def branch_plan(bid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    b = s.get(Branch, bid)
    if not b:
        raise HTTPException(404)
    requested = [m.strip() for m in b.effective_modules.split(",") if m.strip()]
    return _plan_response(request, b.project, b, requested)


# ---------- module sets ----------

def _validate_set(form, p: Project, ms: ModuleSet | None) -> tuple[dict, dict]:
    errors: dict = {}
    name = (form.get("name") or "").strip()
    mods = mv.parse_module_list(form.getlist("modules"), form.get("modules_text") or "")
    if not name:
        errors["name"] = "Required"
    elif any(m.name.lower() == name.lower() and m is not ms for m in p.module_sets):
        errors["name"] = "Another module set of this project has that name"
    bad = [m for m in mods if not MODULE_RE.match(m)]
    if bad:
        errors["modules"] = f"Invalid module name(s): {', '.join(bad)}"
    elif not mods:
        errors["modules"] = "Pick at least one module"
    return {"name": name, "description": (form.get("description") or "").strip(),
            "modules": ",".join(mods)}, errors


def _form(request: Request, p: Project, ms: ModuleSet | None, values: dict, errors: dict,
          branch_id: int | None = None, status_code: int = 200):
    b = pick_branch(p, branch_id)
    return render(request, "module_set_form.html", {
        "project": p, "ms": ms, "values": values, "errors": errors,
        "branch": b, "branch_choices": mv.branch_choices(p),
        "chosen": set(m for m in values.get("modules", "").split(",") if m),
    }, status_code=status_code)


@router.get("/projects/{pid}/module-sets/new")
def new_set(pid: int, request: Request, branch_id: int | None = None, s: Session = Depends(get_db),
            user: User = Depends(current_user)):
    p = get_owner(s, pid)
    return _form(request, p, None, {"name": "", "description": "", "modules": ""}, {}, branch_id)


@router.post("/projects/{pid}/module-sets/new")
async def create_set(pid: int, request: Request, s: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    p = get_owner(s, pid)
    form = await request.form()
    data, errors = _validate_set(form, p, None)
    if errors:
        return _form(request, p, None, data, errors, int(form.get("branch_id") or 0) or None, 400)
    ms = ModuleSet(project=p, **data)
    s.add(ms)
    s.flush()
    if form.get("make_default") in ("on", "true", "1"):
        p.module_set = ms
    s.commit()
    flash(request, f"Module set {ms.name} saved.", "success")
    return RedirectResponse(tab_url(p), status_code=303)


@router.get("/projects/{pid}/module-sets/{sid}/edit")
def edit_set(pid: int, sid: int, request: Request, branch_id: int | None = None,
             s: Session = Depends(get_db), user: User = Depends(current_user)):
    p = get_owner(s, pid)
    ms = get_set(p, sid)
    return _form(request, p, ms, {"name": ms.name, "description": ms.description,
                                  "modules": ms.modules}, {}, branch_id)


@router.post("/projects/{pid}/module-sets/{sid}/edit")
async def update_set(pid: int, sid: int, request: Request, s: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    p = get_owner(s, pid)
    ms = get_set(p, sid)
    form = await request.form()
    data, errors = _validate_set(form, p, ms)
    if errors:
        return _form(request, p, ms, data, errors, int(form.get("branch_id") or 0) or None, 400)
    changed = data["modules"] != ms.modules
    for k, v in data.items():
        setattr(ms, k, v)
    users = mv.set_users(s, ms)
    s.commit()
    msg = f"Module set {ms.name} saved."
    if changed and users:
        msg += f" Used by {', '.join(users)}: rebuild those to apply the new list."
    flash(request, msg, "success")
    return RedirectResponse(tab_url(p), status_code=303)


@router.post("/projects/{pid}/module-sets/{sid}/delete")
def delete_set(pid: int, sid: int, request: Request, s: Session = Depends(get_db),
               user: User = Depends(current_user)):
    p = get_owner(s, pid)
    ms = get_set(p, sid)
    users = mv.set_users(s, ms)
    name = ms.name
    if p.module_set_id == ms.id:
        p.module_set = None
    for b in p.branches:
        if b.module_set_id == ms.id:
            b.module_set = None
    s.delete(ms)
    s.commit()
    note = f" {', '.join(users).capitalize()} now use the project default." if users else ""
    flash(request, f"Module set {name} deleted.{note}", "success")
    return RedirectResponse(tab_url(p), status_code=303)


@router.post("/projects/{pid}/module-sets/default")
async def set_default(pid: int, request: Request, s: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    p = get_owner(s, pid)
    form = await request.form()
    sid = int(form.get("module_set_id") or 0)
    p.module_set = get_set(p, sid) if sid else None
    s.commit()
    what = f"module set {p.module_set.name}" if p.module_set else mv.project_default_desc(p)
    flash(request, f"Project default is now {what}. It applies to the next build of branches "
                   "without their own modules choice.", "success")
    return RedirectResponse(tab_url(p), status_code=303)


@router.post("/projects/{pid}/module-sets/top-level")
async def set_from_top_level(pid: int, request: Request, s: Session = Depends(get_db),
                             user: User = Depends(current_user)):
    """One click: a module set of the top-level modules (installing them installs the rest)."""
    p = get_owner(s, pid)
    form = await request.form()
    b = pick_branch(p, int(form.get("branch_id") or 0) or None)
    if b is None:
        flash(request, "No branch to read the modules from.", "error")
        return RedirectResponse(tab_url(p), status_code=303)
    av = mv.available(b)
    top = av.top_level
    if av.error or not top:
        flash(request, f"Could not read the modules of {b.name}: {av.error or 'none found'}", "error")
        return RedirectResponse(tab_url(p), status_code=303)
    base, name, n = "All modules (top-level)", "All modules (top-level)", 2
    existing = {m.name for m in p.module_sets}
    while name in existing:
        name, n = f"{base} {n}", n + 1
    ms = ModuleSet(project=p, name=name, modules=",".join(top),
                   description=f"Installs all {len(av.own)} modules of {b.name} through "
                               f"{len(top)} top-level module(s).")
    s.add(ms)
    s.commit()
    flash(request, f"Module set {name} created: {', '.join(top)}.", "success")
    return RedirectResponse(tab_url(p), status_code=303)
