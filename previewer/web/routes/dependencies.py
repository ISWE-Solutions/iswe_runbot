"""Project dependency repos (Dependencies tab) and the GitHub lookups behind its dropdowns."""
import hashlib
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy.orm import Session

from ... import deps as builddeps
from ... import github
from ...db import get_db
from ...models import Branch, Project, ProjectDependency, User
from .. import accounts
from ..deps import admin, developer, flash, render
from ..views import version_choices

router = APIRouter()

MODULE_RE = re.compile(r"^[a-z0-9_]+$")
CACHE_TTL = 300
_branch_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}


def _token_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def branch_names(repo_url: str, token: str) -> list[str]:
    """Branch names of a repo, cached ~5 minutes per (repo, token)."""
    key = (github.clone_url(repo_url), _token_key(token))
    hit = _branch_cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    names = sorted(b.name for b in github.list_branches(repo_url, token))
    _branch_cache[key] = (time.time(), names)
    return names


def get_project(s: Session, pid: int) -> Project:
    p = s.get(Project, pid)
    if not p:
        raise HTTPException(404)
    return p


def get_dep(s: Session, p: Project, did: int) -> ProjectDependency:
    d = s.get(ProjectDependency, did)
    if not d or d.project_id != p.id:
        raise HTTPException(404)
    return d


def _form_token(s: Session, form, p: Project) -> str:
    """Token for a GitHub lookup from the form: the chosen account, else the project's."""
    return accounts.token_for_choice(s, form.get("credential_id"), accounts.project_account(p))


def _form_ctx(s: Session, p: Project, dep: ProjectDependency | None, values: dict, errors: dict,
              branches: list[str] | None = None) -> dict:
    return {"project": p, "dep": dep, "values": values, "errors": errors,
            "versions": version_choices(s), "branches": branches,
            "accounts": accounts.all_accounts(s),
            "project_account": accounts.account_name(accounts.project_account(p))}


def _values(dep: ProjectDependency | None) -> dict:
    if not dep:
        return {"repo_url": "", "module_name": "", "branch_map": {}, "credential_id": ""}
    return {"repo_url": dep.repo_url, "module_name": dep.module_name, "branch_map": dict(dep.branch_map or {}),
            "credential_id": dep.credential_id or ""}


def _validate(s: Session, p: Project, dep: ProjectDependency | None, form) -> tuple[dict, dict]:
    errors: dict = {}
    repo_url = (form.get("repo_url") or "").strip()
    module = (form.get("module_name") or "").strip()
    try:
        github.parse_repo(repo_url)
    except github.GitHubError as e:
        errors["repo_url"] = str(e)
    else:
        if github.clone_url(repo_url) == github.clone_url(p.repo_url):
            errors["repo_url"] = "That is the project's own repository"
        for other in p.dependencies:
            if other is not dep and github.clone_url(other.repo_url) == github.clone_url(repo_url):
                errors["repo_url"] = "This repository is already a dependency"
    if module and not MODULE_RE.match(module):
        errors["module_name"] = "Lowercase letters, digits and _ only"
    # Keep mappings for versions not shown (disabled versions), replace the shown ones.
    branch_map = dict(dep.branch_map or {}) if dep else {}
    for v in version_choices(s):
        value = (form.get(f"branch_{v}") or "").strip()
        if value:
            branch_map[v] = value
        else:
            branch_map.pop(v, None)
    credential_id, err = accounts.parse_choice(s, form.get("credential_id"))
    if err:
        errors["credential_id"] = err
    return {"repo_url": repo_url, "module_name": module, "branch_map": branch_map,
            "credential_id": credential_id}, errors


@router.get("/projects/{pid}/deps/new")
def new_dep(pid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(admin)):
    p = get_project(s, pid)
    return render(request, "dependency_form.html", _form_ctx(s, p, None, _values(None), {}))


@router.get("/projects/{pid}/deps/{did}/edit")
def edit_dep(pid: int, did: int, request: Request, s: Session = Depends(get_db),
             user: User = Depends(admin)):
    p = get_project(s, pid)
    d = get_dep(s, p, did)
    return render(request, "dependency_form.html", _form_ctx(s, p, d, _values(d), {}))


async def _save(pid: int, did: int | None, request: Request, s: Session):
    p = get_project(s, pid)
    d = get_dep(s, p, did) if did else None
    form = await request.form()
    data, errors = _validate(s, p, d, form)
    if errors:
        return render(request, "dependency_form.html", _form_ctx(s, p, d, data, errors), status_code=400)
    if d is None:
        d = ProjectDependency(project_id=p.id, position=max((x.position for x in p.dependencies), default=-1) + 1)
        s.add(d)
    d.repo_url, d.module_name, d.branch_map = data["repo_url"], data["module_name"], data["branch_map"]
    d.credential_id = data["credential_id"]
    s.commit()
    flash(request, f"Dependency {d.root_module_name} saved. It applies to the next build of each "
                   "branch; use Rebuild to apply it now.", "success")
    return RedirectResponse(f"/projects/{p.id}?tab=deps", status_code=303)


@router.post("/projects/{pid}/deps/new")
async def create_dep(pid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(admin)):
    return await _save(pid, None, request, s)


@router.post("/projects/{pid}/deps/{did}/edit")
async def update_dep(pid: int, did: int, request: Request, s: Session = Depends(get_db),
                     user: User = Depends(admin)):
    return await _save(pid, did, request, s)


@router.post("/projects/{pid}/deps/{did}/delete")
def delete_dep(pid: int, did: int, request: Request, s: Session = Depends(get_db),
               user: User = Depends(admin)):
    p = get_project(s, pid)
    d = get_dep(s, p, did)
    for b in p.branches:
        if str(d.id) in (b.dependency_branches or {}):
            b.dependency_branches = {k: v for k, v in b.dependency_branches.items() if k != str(d.id)}
    name = d.root_module_name
    s.delete(d)
    s.commit()
    flash(request, f"Dependency {name} removed.", "success")
    return RedirectResponse(f"/projects/{p.id}?tab=deps", status_code=303)


@router.post("/projects/{pid}/deps/{did}/move")
async def move_dep(pid: int, did: int, request: Request, s: Session = Depends(get_db),
                   user: User = Depends(admin)):
    p = get_project(s, pid)
    d = get_dep(s, p, did)
    direction = (await request.form()).get("dir")
    order = list(p.dependencies)
    i = order.index(d)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(order):
        order[i], order[j] = order[j], order[i]
        for pos, x in enumerate(order):
            x.position = pos
        s.commit()
    return RedirectResponse(f"/projects/{p.id}?tab=deps", status_code=303)


@router.post("/projects/{pid}/deps/branches")
async def load_dep_branches(pid: int, request: Request, s: Session = Depends(get_db),
                            user: User = Depends(admin)):
    """HTMX: the per-version branch dropdowns, filled from GitHub."""
    p = get_project(s, pid)
    form = await request.form()
    did = form.get("dep_id")
    d = get_dep(s, p, int(did)) if did and str(did).isdigit() else None
    values = {"branch_map": {v: (form.get(f"branch_{v}") or "") for v in version_choices(s)}}
    repo_url = (form.get("repo_url") or "").strip()
    try:
        names = branch_names(repo_url, _form_token(s, form, p))
        error = None
    except github.GitHubError as e:
        names, error = None, str(e)
    except Exception as e:  # noqa: BLE001
        names, error = None, f"Could not reach GitHub: {e.__class__.__name__}"
    return render(request, "_dep_branch_selects.html", {
        "versions": version_choices(s), "values": values, "branches": names, "load_error": error,
    })


@router.post("/projects/{pid}/deps/test")
async def test_dep(pid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(admin)):
    p = get_project(s, pid)
    form = await request.form()
    did = form.get("dep_id")
    d = get_dep(s, p, int(did)) if did and str(did).isdigit() else None
    try:
        info = github.test_connection((form.get("repo_url") or "").strip(), _form_token(s, form, p))
    except github.GitHubError as e:
        return HTMLResponse(f'<p class="text-sm text-red-700">✗ {escape(str(e))}</p>')
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(f'<p class="text-sm text-red-700">✗ Could not reach GitHub: {escape(e.__class__.__name__)}</p>')
    return HTMLResponse(f'<p class="text-sm text-green-700">✓ Connected to {escape(info["full_name"])} '
                        f'(default branch {escape(info["default_branch"])})</p>')


@router.post("/branches/{bid}/dep-branches/{did}")
def load_branch_override_options(bid: int, did: int, request: Request, s: Session = Depends(get_db),
                                 user: User = Depends(developer)):
    """HTMX: dropdown of one dependency repo's branches for the branch settings page."""
    b = s.get(Branch, bid)
    if not b:
        raise HTTPException(404)
    d = get_dep(s, b.project, did)
    try:
        names, error = branch_names(d.repo_url, builddeps.dependency_token(d)), None
    except github.GitHubError as e:
        names, error = None, str(e)
    except Exception as e:  # noqa: BLE001
        names, error = None, f"Could not reach GitHub: {e.__class__.__name__}"
    return render(request, "_dep_override_select.html", {
        "branch": b, "dep": d, "names": names, "load_error": error,
        "current": (b.dependency_branches or {}).get(str(d.id), ""),
        "default": d.branch_for(b.effective_version),
    })
