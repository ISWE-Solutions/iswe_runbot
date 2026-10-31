import re

import httpx

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ... import appsettings, github, jobs
from ...config import get_settings
from ...db import get_db
from ...models import JOB_STATUSES, JOB_TYPES, Branch, Build, DbTemplate, Job, OdooVersion, Project, SchedulerState, User
from ...security import decrypt, encrypt, hash_password
from .. import accounts, system_view
from ..deps import current_user, flash, paginate, paginate_list, render

router = APIRouter(prefix="/admin")

VERSION_RE = re.compile(r"^\d+\.\d+$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")


# ---------- Odoo versions ----------

@router.get("/versions")
def versions(request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = s.scalars(select(OdooVersion)).all()
    rows = sorted(rows, key=lambda v: [int(x) for x in v.version.split(".")])
    status = {v.version: accounts.enterprise_status(s, v.version) for v in rows if v.enabled}
    return render(request, "admin_versions.html", {
        "versions": rows, "enterprise": status, "accounts": accounts.all_accounts(s),
        "default_account_label": accounts.default_choice_label(s),
        "fallback_repo": get_settings().enterprise_repo_url,
    })


@router.post("/versions")
def add_version(request: Request, version: str = Form(...), docker_image: str = Form(...),
                demo_flag_on: str = Form(""), demo_flag_off: str = Form(""),
                s: Session = Depends(get_db), user: User = Depends(current_user)):
    version, docker_image = version.strip(), docker_image.strip()
    if not VERSION_RE.match(version) or not docker_image:
        flash(request, "Version must look like 18.0 and needs a Docker image.", "error")
    elif s.get(OdooVersion, version):
        flash(request, f"Odoo {version} already exists.", "error")
    else:
        on, off = demo_flag_on.strip(), demo_flag_off.strip()
        if not on and not off:  # blank: the style Odoo uses for that version
            on, off = ("--with-demo", "") if int(version.split(".")[0]) >= 19 else ("", "--without-demo=all")
        problem = demo_flags_error(version, on, off)
        if problem:
            flash(request, problem, "error")
            return RedirectResponse("/admin/versions", status_code=303)
        s.add(OdooVersion(version=version, docker_image=docker_image, demo_flag_on=on,
                          demo_flag_off=off, enabled=True))
        s.commit()
        flash(request, f"Odoo {version} added. Pull its image before the first build.", "success")
    return RedirectResponse("/admin/versions", status_code=303)


def demo_flags_error(version: str, on: str, off: str) -> str | None:
    """Odoo 19+ loads demo data only with --with-demo; 18 and older load it unless told
    --without-demo. Saving the other style silently breaks the demo choice of every build."""
    try:
        major = int(version.split(".")[0])
    except ValueError:
        return None
    if major >= 19 and "--with-demo" not in on:
        return f"Odoo {version} needs --with-demo under Demo on (it loads no demo data by default)."
    if major <= 18 and "--without-demo" not in off:
        return f"Odoo {version} needs --without-demo=all under Demo off (it loads demo data by default)."
    return None


@router.post("/versions/{version}")
def edit_version(version: str, request: Request, docker_image: str = Form(...),
                 demo_flag_on: str = Form(""), demo_flag_off: str = Form(""), enabled: str = Form(""),
                 s: Session = Depends(get_db), user: User = Depends(current_user)):
    v = s.get(OdooVersion, version)
    if not v:
        raise HTTPException(404)
    problem = demo_flags_error(version, demo_flag_on.strip(), demo_flag_off.strip())
    if problem:
        flash(request, problem, "error")
        return RedirectResponse("/admin/versions", status_code=303)
    v.docker_image, v.demo_flag_on, v.demo_flag_off = docker_image.strip(), demo_flag_on.strip(), demo_flag_off.strip()
    v.enabled = enabled in ("on", "true", "1")
    s.commit()
    flash(request, f"Odoo {version} saved.", "success")
    return RedirectResponse("/admin/versions", status_code=303)


def _enterprise_form(s: Session, v: OdooVersion, form) -> str | None:
    """Apply the Enterprise source fields to v; returns an error message or None."""
    url = (form.get("enterprise_repo_url") or "").strip()
    if url:
        try:
            github.parse_repo(url)
        except github.GitHubError as e:
            return str(e)
        url = github.clone_url(url)
    cid, err = accounts.parse_choice(s, form.get("enterprise_credential_id"))
    if err:
        return err
    v.enterprise_repo_url = url
    v.enterprise_branch = (form.get("enterprise_branch") or "").strip()
    v.enterprise_credential_id = cid
    return None


@router.post("/versions/{version}/enterprise")
async def save_enterprise_source(version: str, request: Request, s: Session = Depends(get_db),
                                 user: User = Depends(current_user)):
    v = s.get(OdooVersion, version)
    if not v:
        raise HTTPException(404)
    err = _enterprise_form(s, v, await request.form())
    if err:
        s.rollback()
        flash(request, f"Odoo {version} Enterprise source not saved: {err}", "error")
    else:
        s.commit()
        st = accounts.enterprise_status(s, version, fresh=True)
        if st["ok"]:
            name = "/".join(github.parse_repo(st["repo_url"]))
            flash(request, f"Odoo {version} Enterprise source saved: {name} @ {st['branch']} is "
                           f"readable with {st['account']}.", "success")
        else:
            flash(request, f"Odoo {version} Enterprise source saved, but: {st['message']}", "error")
    return RedirectResponse("/admin/versions", status_code=303)


@router.post("/versions/{version}/enterprise/check")
async def check_enterprise_source(version: str, request: Request, s: Session = Depends(get_db),
                                  user: User = Depends(current_user)):
    """HTMX: check the form's (unsaved) Enterprise source for this version on GitHub."""
    v = s.get(OdooVersion, version)
    if not v:
        raise HTTPException(404)
    err = _enterprise_form(s, v, await request.form())
    if err:
        s.rollback()
        return HTMLResponse(f'<p class="text-xs text-red-700">✗ {escape(err)}</p>')
    st = accounts.enterprise_status(s, version, fresh=True)
    s.rollback()  # a check never saves
    if st["ok"] is None:
        return HTMLResponse(f'<p class="text-xs text-gray-600">{escape(st["message"])}</p>')
    if st["ok"]:
        name = "/".join(github.parse_repo(st["repo_url"]))
        return HTMLResponse(f'<p class="text-xs text-green-700">✓ {escape(name)} @ {escape(st["branch"])} '
                            f'is readable with {escape(st["account"])}</p>')
    return HTMLResponse(f'<p class="text-xs text-red-700">✗ {escape(st["message"])}</p>')


@router.post("/versions/{version}/pull")
def pull_version(version: str, request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    v = s.get(OdooVersion, version)
    if not v:
        raise HTTPException(404)
    jobs.enqueue(s, "pull", {"version": v.version})
    s.commit()
    flash(request, f"Pulling {v.docker_image} in the background.", "info")
    return RedirectResponse("/admin/versions", status_code=303)


@router.post("/versions/{version}/delete")
def delete_version(version: str, request: Request, s: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    v = s.get(OdooVersion, version)
    if not v:
        raise HTTPException(404)
    projects = s.scalars(select(Project.name).where(Project.default_odoo_version == version)).all()
    branches = s.scalars(select(Branch).where(Branch.odoo_version_override == version)).all()
    if projects or branches:
        used = [f"project {n}" for n in projects] + [f"{b.project.name} / {b.name}" for b in branches]
        flash(request, f"Odoo {version} is still used by {', '.join(used)}; change those first.", "error")
        return RedirectResponse("/admin/versions", status_code=303)
    s.delete(v)
    s.commit()
    flash(request, f"Odoo {version} removed. Its builds stay in the history.", "success")
    return RedirectResponse("/admin/versions", status_code=303)


# ---------- users ----------

@router.get("/users")
def users(request: Request, q: str = "", page: int = 1,
          s: Session = Depends(get_db), user: User = Depends(current_user)):
    stmt = select(User).order_by(User.email)
    if q:
        stmt = stmt.where(or_(User.email.ilike(f"%{q}%"), User.name.ilike(f"%{q}%")))
    return render(request, "admin_users.html", {"page": paginate(s, stmt, page), "q": q})


@router.post("/users")
def invite(request: Request, email: str = Form(...), name: str = Form(""),
           password: str = Form(...), s: Session = Depends(get_db), user: User = Depends(current_user)):
    email = email.strip().lower()
    if not EMAIL_RE.match(email) or len(password) < 8:
        flash(request, "Need a valid email and a password of at least 8 characters.", "error")
    elif s.scalars(select(User).where(User.email == email)).first():
        flash(request, f"{email} already has an account.", "error")
    else:
        s.add(User(email=email, name=name.strip(), password_hash=hash_password(password)))
        s.commit()
        flash(request, f"{email} added. Share the password with them securely.", "success")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{uid}")
def update_user(uid: int, request: Request, active: str = Form(""),
                password: str = Form(""), s: Session = Depends(get_db), user: User = Depends(current_user)):
    u = s.get(User, uid)
    if not u:
        raise HTTPException(404)
    is_active = active in ("on", "true", "1")
    if u.id == user.id and not is_active:
        flash(request, "You cannot deactivate yourself.", "error")
        return RedirectResponse("/admin/users", status_code=303)
    u.active = is_active
    if password:
        if len(password) < 8:
            flash(request, "Password must be at least 8 characters.", "error")
            return RedirectResponse("/admin/users", status_code=303)
        u.password_hash = hash_password(password)
    s.commit()
    flash(request, f"{u.email} saved.", "success")
    return RedirectResponse("/admin/users", status_code=303)


# ---------- system ----------

SYSTEM_TABS = ("previews", "queue", "tasks", "templates", "settings", "sign-in", "jobs")


def _system_ctx(s: Session, errors: dict | None = None, form_values: dict | None = None, *,
                tab: str = "previews", page: int = 1, jtype: str = "", jstatus: str = "",
                signin_errors: dict | None = None, signin_form: dict | None = None) -> dict:
    from datetime import datetime, timezone
    builds = dict(s.execute(select(Build.status, func.count()).group_by(Build.status)).all())
    queue = s.scalars(select(Job).where(Job.status.in_(("queued", "running"))).order_by(Job.id)).all()
    now = datetime.now(timezone.utc)
    current = {k: appsettings.get(s, k) for k in appsettings.defaults()}
    beat = system_view.heartbeat()
    previews = [p for p in beat.get("previews") or [] if isinstance(p, dict)]
    previews.sort(key=lambda p: p.get("mem_mb") or 0, reverse=True)
    tasks = s.scalars(select(SchedulerState).order_by(SchedulerState.name)).all()
    templates = s.scalars(select(DbTemplate).order_by(DbTemplate.version, DbTemplate.key)).all()
    pg = None
    if tab == "jobs":
        stmt = select(Job).order_by(Job.id.desc())
        if jtype:
            stmt = stmt.where(Job.type == jtype)
        if jstatus:
            stmt = stmt.where(Job.status == jstatus)
        pg = paginate(s, stmt, page)
    elif tab == "previews":
        pg = paginate_list(previews, page)
    elif tab == "tasks":
        pg = paginate_list(tasks, page)
    elif tab == "templates":
        pg = paginate_list(templates, page)
    mem = [p["mem_mb"] for p in previews if isinstance(p.get("mem_mb"), (int, float))]
    return {
        "beat": beat, "previews": previews, "preview_mem_mb": round(sum(mem)) if mem else None,
        "builds": builds,
        "running": builds.get("running", 0), "sleeping": builds.get("sleeping", 0),
        "max_running": system_view.running_cap(s, current),
        "max_running_setting": int(current["max_running_previews"]),
        "queue": [(j, int((now - j.created_at).total_seconds())) for j in queue],
        "tasks": tasks,
        "templates": templates,
        "tab": tab if tab in SYSTEM_TABS else "previews",
        "pg": pg, "jtype": jtype, "jstatus": jstatus,
        "job_types": JOB_TYPES, "job_statuses": JOB_STATUSES,
        "failed_tasks": sum(1 for t in tasks if not t.last_ok),
        "groups": system_view.GROUPS,
        "values": {**current, **(form_values or {})},
        "slack_set": bool(current.get("slack_webhook_url_enc")),
        "errors": errors or {},
        "disk_warning": system_view.disk_warning(s),
        "signin": system_view.signin(s) if tab == "sign-in" else {},
        "signin_errors": signin_errors or {}, "signin_form": signin_form or {},
    }


@router.get("/system")
def system(request: Request, tab: str = "previews", page: int = 1, jtype: str = "", jstatus: str = "",
           s: Session = Depends(get_db), user: User = Depends(current_user)):
    return render(request, "admin_system.html",
                  _system_ctx(s, tab=tab, page=page, jtype=jtype, jstatus=jstatus))


@router.post("/system/settings")
async def save_system_settings(request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    current = {k: appsettings.get(s, k) for k in appsettings.defaults()}
    values, errors = system_view.validate(form, current)
    slack = (form.get("slack_webhook_url") or "").strip()
    if slack and not slack.startswith("https://"):
        errors["slack_webhook_url_enc"] = "Must be an https:// URL (Slack incoming webhook)"
    if errors:
        return render(request, "admin_system.html", _system_ctx(s, errors, dict(form), tab="settings"),
                      status_code=400)
    for k, v in values.items():
        appsettings.put(s, k, v)
    if form.get("slack_clear") in ("on", "1"):
        appsettings.put(s, "slack_webhook_url_enc", "")
    elif slack:
        appsettings.put(s, "slack_webhook_url_enc", encrypt(slack))
    s.commit()
    flash(request, "Settings saved. The worker picks them up on its next check"
                   " (the number of build slots after a worker restart).", "success")
    return RedirectResponse("/admin/system?tab=settings", status_code=303)


@router.post("/system/slack-test", response_class=HTMLResponse)
def slack_test(request: Request, s: Session = Depends(get_db), user: User = Depends(current_user)):
    enc = appsettings.get(s, "slack_webhook_url_enc")
    if not enc:
        return '<p class="text-sm text-amber-700">No Slack webhook URL is saved yet.</p>'
    try:
        r = httpx.post(decrypt(enc), json={"text": f"Test message from the Previewer at "
                                                   f"{get_settings().dashboard_url} (sent by {user.email})"},
                       timeout=10)
    except httpx.HTTPError as e:
        return f'<p class="text-sm text-red-700">Could not reach Slack: {escape(type(e).__name__)}</p>'
    if r.status_code >= 300:
        return f'<p class="text-sm text-red-700">Slack answered {r.status_code}: {escape(r.text[:200])}</p>'
    return '<p class="text-sm text-green-700">✓ Sent. Check the Slack channel.</p>'


@router.post("/system/sign-in/{provider}")
async def save_sign_in(provider: str, request: Request, s: Session = Depends(get_db),
                       user: User = Depends(current_user)):
    from .. import sso
    if provider not in system_view.SIGNIN_FIELDS:
        raise HTTPException(404)
    form = await request.form()
    label = sso.LABELS[provider]
    errors = system_view.save_signin(s, provider, form)
    if errors:
        s.rollback()
        typed = {k: str(form.get(k) or "") for k, *_ in system_view.SIGNIN_FIELDS[provider]}
        flash(request, f"{label} sign-in not saved: check the highlighted fields.", "error")
        return render(request, "admin_system.html",
                      _system_ctx(s, tab="sign-in", signin_errors=errors, signin_form=typed),
                      status_code=400)
    s.commit()
    need = sso.missing(provider, sso.effective(s))
    if need:
        flash(request, f"{label} sign-in saved. Still needed: {', '.join(need)}.", "info")
    else:
        flash(request, f"{label} sign-in saved and ready: the login page shows the button now.", "success")
    return RedirectResponse("/admin/system?tab=sign-in", status_code=303)
