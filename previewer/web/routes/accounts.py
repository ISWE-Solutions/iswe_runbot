"""Admin: Git credentials the system pulls with (one is the default)."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ... import github
from ...db import get_db
from ...models import GithubCredential, User, utcnow
from ...security import decrypt, encrypt
from .. import accounts
from ..deps import admin, developer, flash, paginate, render

router = APIRouter(prefix="/admin/github-accounts")


def get_account(s: Session, cid: int) -> GithubCredential:
    c = s.get(GithubCredential, cid)
    if not c:
        raise HTTPException(404)
    return c


@router.get("")
def list_accounts(request: Request, q: str = "", page: int = 1, s: Session = Depends(get_db),
                  user: User = Depends(developer)):
    stmt = select(GithubCredential).order_by(GithubCredential.is_default.desc(), GithubCredential.name)
    if q:
        stmt = stmt.where(or_(GithubCredential.name.ilike(f"%{q}%"),
                              GithubCredential.username.ilike(f"%{q}%")))
    pg = paginate(s, stmt, page)
    return render(request, "admin_accounts.html", {
        "page": pg, "q": q, "usage": {c.id: accounts.usage(s, c) for c in pg.items},
    })


def _validate(s: Session, form, account: GithubCredential | None) -> tuple[dict, dict]:
    errors = {}
    name = (form.get("name") or "").strip()
    username = (form.get("username") or "").strip()
    token = (form.get("token") or "").strip()
    if not name:
        errors["name"] = "Required"
    else:
        clash = s.scalars(select(GithubCredential).where(GithubCredential.name == name)).first()
        if clash and clash is not account:
            errors["name"] = "Another account has this name"
    if account is None and not token:
        errors["token"] = "Required for a new account"
    return {"name": name, "username": username, "token": token}, errors


def _form(request: Request, account: GithubCredential | None, values: dict, errors: dict, status: int = 200):
    return render(request, "account_form.html", {"account": account, "values": values, "errors": errors},
                  status_code=status)


@router.get("/new")
def new_account(request: Request, user: User = Depends(admin)):
    return _form(request, None, {"name": "", "username": ""}, {})


@router.post("/new")
async def create_account(request: Request, s: Session = Depends(get_db), user: User = Depends(admin)):
    data, errors = _validate(s, await request.form(), None)
    if errors:
        return _form(request, None, data, errors, 400)
    first = s.scalars(select(GithubCredential)).first() is None
    c = GithubCredential(name=data["name"], username=data["username"], token_enc=encrypt(data["token"]),
                         is_default=first)
    s.add(c)
    s.commit()
    flash(request, f"Git credential {c.name} added{' as the default' if first else ''}. "
                   "Press Test to check it.", "success")
    return RedirectResponse("/admin/github-accounts", status_code=303)


@router.get("/{cid}/edit")
def edit_account(cid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(admin)):
    c = get_account(s, cid)
    return _form(request, c, {"name": c.name, "username": c.username}, {})


@router.post("/{cid}/edit")
async def update_account(cid: int, request: Request, s: Session = Depends(get_db),
                         user: User = Depends(admin)):
    c = get_account(s, cid)
    data, errors = _validate(s, await request.form(), c)
    if errors:
        return _form(request, c, data, errors, 400)
    c.name, c.username = data["name"], data["username"]
    if data["token"]:
        c.token_enc = encrypt(data["token"])
        c.checked_login = c.checked_at = c.check_error = None
    s.commit()
    accounts.clear_enterprise_cache()
    flash(request, f"Git credential {c.name} saved.", "success")
    return RedirectResponse("/admin/github-accounts", status_code=303)


@router.post("/{cid}/default")
def make_default(cid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(admin)):
    c = get_account(s, cid)
    for other in s.scalars(select(GithubCredential).where(GithubCredential.is_default.is_(True))):
        other.is_default = False
    s.flush()
    c.is_default = True
    s.commit()
    accounts.clear_enterprise_cache()
    flash(request, f"{c.name} is now the default Git credential: projects, dependencies and "
                   "Enterprise sources set to Default use it from the next build.", "success")
    return RedirectResponse("/admin/github-accounts", status_code=303)


@router.post("/{cid}/delete")
def delete_account(cid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(admin)):
    c = get_account(s, cid)
    if c.is_default:
        flash(request, f"{c.name} is the default account. Make another account the default first.", "error")
        return RedirectResponse("/admin/github-accounts", status_code=303)
    u = accounts.usage(s, c)
    if u["explicit"]:
        where = ([f"project {p.name}" for p in u["projects"]]
                 + [f"dependency {d.root_module_name} of {d.project.name}" for d in u["dependencies"]]
                 + [f"Enterprise source of Odoo {v.version}" for v in u["versions"]])
        flash(request, f"{c.name} is still used by {', '.join(where)}. Switch those first.", "error")
        return RedirectResponse("/admin/github-accounts", status_code=303)
    name = c.name
    s.delete(c)
    s.commit()
    flash(request, f"Git credential {name} deleted.", "success")
    return RedirectResponse("/admin/github-accounts", status_code=303)


@router.post("/{cid}/test")
async def test_account(cid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(admin)):
    """HTMX: who the token authenticates as, its rate limit, and optionally a repo's readability."""
    c = get_account(s, cid)
    form = await request.form()
    repo_url = (form.get("repo_url") or "").strip()
    token = decrypt(c.token_enc)
    lines = []
    try:
        me = accounts.whoami(token)
    except github.GitHubError as e:
        c.checked_login, c.check_error = None, str(e)
        lines.append(("red", f"✗ {e}"))
    except Exception as e:  # noqa: BLE001  (network errors)
        c.checked_login, c.check_error = None, f"Could not reach GitHub: {e.__class__.__name__}"
        lines.append(("red", f"✗ {c.check_error}"))
    else:
        c.checked_login, c.check_error = me["login"], None
        lines.append(("green", f"✓ Authenticated as {me['login']}"))
        if me["rate_remaining"] is not None:
            lines.append(("gray", f"API rate limit: {me['rate_remaining']} of {me['rate_limit']} requests left this hour"))
        if me["scopes"]:
            lines.append(("gray", f"Classic token scopes: {me['scopes']}"))
        if c.username and c.username.lower() != me["login"].lower():
            lines.append(("amber", f"Username is set to {c.username} but the token belongs to {me['login']}"))
    c.checked_at = utcnow()
    s.commit()
    if repo_url:
        try:
            info = github.test_connection(repo_url, token)
            lines.append(("green", f"✓ Can read {info['full_name']} ({'private' if info['private'] else 'public'}, "
                                   f"default branch {info['default_branch']})"))
        except github.GitHubError as e:
            lines.append(("red", f"✗ {repo_url}: {e}"))
        except Exception as e:  # noqa: BLE001
            lines.append(("red", f"✗ Could not reach GitHub: {e.__class__.__name__}"))
    colours = {"green": "text-green-700", "red": "text-red-700", "amber": "text-amber-700", "gray": "text-gray-600"}
    body = "".join(f'<p class="text-xs {colours[k]}">{escape(t)}</p>' for k, t in lines)
    return HTMLResponse(f'<div class="space-y-0.5">{body}</div>')


alias_router = APIRouter()


@alias_router.get("/admin/credentials")
def credentials_alias():
    """Short address for the Git credentials page (the sidebar links here)."""
    return RedirectResponse("/admin/github-accounts", status_code=303)
