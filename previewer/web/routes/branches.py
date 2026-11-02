from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import deps as builddeps
from ... import jobs
from ...db import get_db
from ...models import Branch, Build, OdooVersion, User
from .. import selection
from ..deps import current_user, flash, paginate, render
from .board import board_ctx
from .projects import _parse_modules, branches_ctx

router = APIRouter()


def get_branch(s: Session, bid: int) -> Branch:
    b = s.get(Branch, bid)
    if not b:
        raise HTTPException(404)
    return b


def _done(request: Request, b: Branch, s: Session):
    """HTMX callers get the refreshed branches table; plain forms are redirected."""
    if request.headers.get("hx-request"):
        target = request.headers.get("hx-target") or ""
        if target == "board-rows":
            qp = request.query_params
            return render(request, "_board_rows.html",
                          board_ctx(request, s, qp.get("project"), qp.get("show", "all"),
                                    qp.get("q", ""), int(qp.get("page", 1) or 1)))
        if target.startswith("branches"):
            qp = request.query_params
            return render(request, "_branches_table.html",
                          branches_ctx(s, b.project, qp.get("q", ""), qp.get("status", ""),
                                       int(qp.get("page", 1) or 1), qp.get("sel", "")))
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    back = request.headers.get("referer") or f"/projects/{b.project_id}"
    return RedirectResponse(back, status_code=303)


@router.get("/branches/{bid}")
def branch_page(bid: int, request: Request, page: int = 1, tab: str = "overview",
                s: Session = Depends(get_db), user: User = Depends(current_user)):
    b = get_branch(s, bid)
    if b.project.is_combo:
        return RedirectResponse(f"/combos/{b.project_id}", status_code=303)
    builds = paginate(s, select(Build).where(Build.branch_id == b.id).order_by(Build.id.desc()), page)
    return render(request, "branch.html", {
        "branch": b, "project": b.project, "builds": builds,
        "tab": tab if tab in ("overview", "builds", "plan", "deps") else "overview",
    })


@router.post("/branches/{bid}/rebuild")
def rebuild(bid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    b = get_branch(s, bid)
    if not b.enabled:
        flash(request, f"{b.name} is not selected for building; select it first.", "error")
    elif selection.queue(request, s, b):
        flash(request, f"Rebuild of {b.name} queued.", "success")
    s.commit()
    return _done(request, b, s)


@router.post("/branches/{bid}/pull-rebuild")
def pull_rebuild(bid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    b = get_branch(s, bid)
    if not b.enabled:
        flash(request, f"{b.name} is not selected for building; select it first.", "error")
    elif msg := selection.pull_latest(request, s, b):
        flash(request, msg, "success")
    s.commit()
    return _done(request, b, s)


@router.post("/branches/{bid}/version")
def change_version(bid: int, request: Request, odoo_version: str = Form(...),
                   s: Session = Depends(get_db), user: User = Depends(current_user)):
    b = get_branch(s, bid)
    v = s.get(OdooVersion, odoo_version)
    if not v or not v.enabled:
        flash(request, f"Odoo {odoo_version} is not enabled.", "error")
        return _done(request, b, s)
    before = b.effective_version
    b.odoo_version_override = None if odoo_version == b.project.default_odoo_version else odoo_version
    s.flush()
    if b.effective_version != before and b.enabled:
        if selection.queue_checked(request, s, b) == "queued":
            flash(request, f"{b.name} now builds on Odoo {b.effective_version} (fresh database).", "success")
    s.commit()
    return _done(request, b, s)


@router.post("/branches/{bid}/stop")
def stop(bid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    b = get_branch(s, bid)
    live = b.live_build
    if live:
        jobs.enqueue(s, "stop", {"build_id": live.id}, branch_id=b.id, build_id=live.id)
        flash(request, f"Stopping {b.name}.", "info")
    s.commit()
    return _done(request, b, s)


@router.post("/branches/{bid}/wake")
def wake(bid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    """Sleeping -> wake job; stopped -> start job (latest build that still has a container)."""
    b = get_branch(s, bid)
    target = next((x for x in b.builds if x.status in ("sleeping", "stopped") and x.container_id), None)
    if target:
        kind = "wake" if target.status == "sleeping" else "start"
        jobs.enqueue(s, kind, {"build_id": target.id}, branch_id=b.id, build_id=target.id)
        flash(request, f"Starting {b.name}.", "info")
    else:
        flash(request, f"{b.name} has no stopped preview to start; use Rebuild.", "error")
    s.commit()
    return _done(request, b, s)


@router.post("/branches/{bid}/sticky")
def toggle_sticky(bid: int, request: Request, s: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    b = get_branch(s, bid)
    b.sticky = not b.sticky
    s.commit()
    return _done(request, b, s)


@router.post("/branches/{bid}/auto-rebuild")
def toggle_auto_rebuild(bid: int, request: Request, s: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    b = get_branch(s, bid)
    b.auto_rebuild = not b.auto_rebuild
    flash(request, f"{b.name}: " + ("new commits rebuild it automatically again." if b.auto_rebuild
          else "auto-rebuild paused; new commits are not built (Rebuild still works)."), "info")
    s.commit()
    return _done(request, b, s)


@router.post("/branches/{bid}/builds/delete")
async def delete_builds(bid: int, request: Request, s: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    """Bulk delete from the branch page: ticked ids, or every failed build (all_failed=1)."""
    from .builds import remove_build
    b = get_branch(s, bid)
    form = await request.form()
    if form.get("all_failed"):
        chosen = [x for x in b.builds if x.status == "failed"]
    else:
        ids = {int(x) for x in form.getlist("ids") if str(x).isdigit()}
        chosen = [x for x in b.builds if x.id in ids]
    if not chosen:
        flash(request, "Nothing to delete: tick at least one build.", "error")
        return RedirectResponse(f"/branches/{b.id}", status_code=303)
    outcomes = [(x.id, remove_build(s, x)) for x in chosen]
    done = [i for i, o in outcomes if o != "building"]
    refused = [i for i, o in outcomes if o == "building"]
    if done:
        msg = f"Deleted {len(done)} build(s): " + ", ".join(f"#{i}" for i in done) + "."
        if any(o == "live" for _, o in outcomes):
            until = "the next new commit" if b.auto_rebuild else "you press Rebuild"
            msg += f" The {b.name} preview is gone; it will not rebuild until {until}."
        flash(request, msg, "info")
    if refused:
        flash(request, "Still building, not deleted: " + ", ".join(f"#{i}" for i in refused), "error")
    s.commit()
    return RedirectResponse(f"/branches/{b.id}", status_code=303)


@router.post("/branches/{bid}/select")
def select_branch(bid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    """Select for building (also "Check again"): dependency check, then queue if it passes."""
    b = get_branch(s, bid)
    if selection.select(request, s, b) == "queued":
        flash(request, f"{b.name} selected; build queued.", "success")
    s.commit()
    return _done(request, b, s)


@router.post("/branches/{bid}/unselect")
def unselect_branch(bid: int, request: Request, s: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    b = get_branch(s, bid)
    selection.unselect(s, b)
    flash(request, f"{b.name} unselected: it will not be built.", "info")
    s.commit()
    return _done(request, b, s)


@router.post("/branches/{bid}/toggle")
def toggle(bid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    b = get_branch(s, bid)
    return unselect_branch(bid, request, s, user) if b.enabled else select_branch(bid, request, s, user)


@router.post("/branches/{bid}/build-anyway")
def build_anyway(bid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    b = get_branch(s, bid)
    b.enabled = True
    if selection.queue(request, s, b):
        flash(request, f"Building {b.name} without a passing dependency check.", "info")
    s.commit()
    return _done(request, b, s)


@router.post("/branches/{bid}/check")
def check_branch(bid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    """HTMX: the dependency check as a small ✓/✗ list (no state change)."""
    b = get_branch(s, bid)
    return render(request, "_check_results.html", {"branch": b, "results": selection.run_check(b)})


@router.get("/branches/{bid}/settings")
def settings_form(bid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    b = get_branch(s, bid)
    return render(request, "branch_settings.html", _settings_ctx(s, b, {}))


@router.post("/branches/{bid}/settings")
async def save_settings(bid: int, request: Request, s: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    b = get_branch(s, bid)
    form = await request.form()
    errors: dict = {}
    version = (form.get("odoo_version_override") or "").strip() or None
    if version:
        v = s.get(OdooVersion, version)
        if not v or not v.enabled:
            errors["odoo_version_override"] = "Choose an enabled Odoo version"
    mode = form.get("modules_mode") or ("custom" if (form.get("modules_override") or "").strip() else "default")
    modules, module_set = None, None
    if mode == "custom":
        modules = _parse_modules(form.get("modules_override") or "", errors, "modules_override") or None
        if not modules:
            errors["modules_override"] = "Enter at least one module, or choose another option"
    elif mode == "set":
        sid = int(form.get("module_set_id") or 0)
        module_set = next((m for m in b.project.module_sets if m.id == sid), None)
        if module_set is None:
            errors["modules_override"] = "Choose a module set of this project"
    demo = {"default": None, "on": True, "off": False}.get(form.get("demo_data_override") or "default")
    run_tests = {"default": None, "on": True, "off": False}.get(form.get("run_tests_override") or "default")
    if errors:
        return render(request, "branch_settings.html", _settings_ctx(s, b, errors), status_code=400)
    before = (b.effective_version, b.effective_modules, b.effective_demo, b.effective_run_tests, _resolved(b))
    if version == b.project.default_odoo_version:
        version = None
    b.odoo_version_override, b.modules_override, b.demo_data_override = version, modules, demo
    b.module_set = module_set
    b.run_tests_override = run_tests
    dep_ids = {str(d.id) for d in b.project.dependencies}
    b.dependency_branches = {k[4:]: v.strip() for k, v in form.items()
                             if k.startswith("dep_") and k[4:] in dep_ids and v.strip()}
    b.auto_rebuild = form.get("auto_rebuild") in ("on", "true", "1")
    was_enabled = b.enabled
    s.flush()
    changed = (b.effective_version, b.effective_modules, b.effective_demo, b.effective_run_tests,
               _resolved(b)) != before
    if not (form.get("enabled") in ("on", "true", "1")):
        if was_enabled:
            selection.unselect(s, b)
        flash(request, "Settings saved; the branch is not selected for building.", "success")
    elif changed or not was_enabled:
        b.enabled = True
        outcome = selection.queue_checked(request, s, b)
        if outcome == "queued":
            flash(request, f"Settings saved; rebuilding {b.name}.", "success")
        elif outcome == "skipped":
            flash(request, "Settings saved.", "success")
        else:
            s.commit()
            return RedirectResponse(f"/branches/{b.id}/settings", status_code=303)
    else:
        flash(request, "Settings saved.", "success")
    s.commit()
    return RedirectResponse(f"/projects/{b.project_id}", status_code=303)


def _resolved(b: Branch) -> list[tuple[int, str]]:
    return [(r.dependency.id, r.branch) for r in builddeps.resolve(b)]


def _settings_ctx(s: Session, b: Branch, errors: dict) -> dict:
    from ..views import version_choices
    from ..module_view import project_default_desc
    return {
        "branch": b, "project": b.project, "errors": errors,
        "project_default": project_default_desc(b.project),
        "versions": version_choices(s, b.odoo_version_override),
        "resolved": {r.dependency.id: r for r in builddeps.resolve(b)},
        "blocked": selection.blocked(b),
    }
