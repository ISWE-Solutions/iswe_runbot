"""Dashboard app. Requests for preview hosts that reach us (Traefik catch-all: no running
container) get a status page instead of the dashboard."""
import os
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.convertors import Convertor, register_url_convertor
from starlette.middleware.sessions import SessionMiddleware

from ..config import get_settings
from ..db import SessionLocal
from ..models import Branch
from .deps import CSRFError, Forbidden, LoginRequired, csrf_protect, render


class OdooVersionConvertor(Convertor):
    regex = r"\d+\.\d+"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("odoover", OdooVersionConvertor())

from .routes import (  # noqa: E402
    accounts, admin, auth, board, forward_auth, branches, builds, combos, dependencies, friendly, health, modules,
    access, plain, projects, webhooks,
)
from . import failure_view, module_view, selection, system_view, waking  # noqa: E402
from .deps import templates  # noqa: E402
from .views import (  # noqa: E402
    detail_url, error_headline, friendly_path, github_base, served_version, unbuilt_commit,
)

templates.env.globals.update(friendly_path=friendly_path, served_version=served_version,
                             github_base=github_base, error_headline=error_headline,
                             card_style=board.card_style, dep_summary=selection.dep_summary,
                             blocked=selection.blocked, unbuilt_commit=unbuilt_commit,
                             detail_url=detail_url, plan_from_dict=module_view.plan_from_dict,
                             explain_plan=module_view.explain_dict,
                             grouped_install=module_view.grouped_install,
                             modules_source=module_view.modules_source,
                             dependency_lines=module_view.dependency_lines,
                             plain_describe=plain.describe,
                             findings_for=failure_view.findings_for,
                             first_finding=failure_view.first_finding,
                             finding_where=failure_view.where, log_needles=failure_view.log_needles,
                             kind_icon=failure_view.kind_icon, kind_label=failure_view.kind_label,
                             app_setting=system_view.app_setting, disk_banner=system_view.disk_banner)

settings = get_settings()
LOCAL_HOSTS = {"localhost", "127.0.0.1"}
ANY_HOST_PATHS = ("/health", "/static/", "/auth/forward", "/api/webhooks/")

app = FastAPI(title="Odoo Branch Previewer", docs_url=None, redoc_url=None, openapi_url=None,
              dependencies=[Depends(csrf_protect)])


@app.middleware("http")
async def preview_host_dispatch(request: Request, call_next):
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host == settings.domain or host in LOCAL_HOSTS or request.url.path.startswith(ANY_HOST_PATHS):
        return await call_next(request)
    s = SessionLocal()
    try:
        branch = s.scalars(select(Branch).where(Branch.host == host)).first()
        if branch is None:
            return render(request, "preview_unknown.html", {"host": host}, status_code=404)
        live = branch.live_build
        stale = waking.stale_stopped_build(branch) if live is None else None
        if (live and live.status == "sleeping") or stale:
            target = live or stale
            if stale and waking.session_user(s, request.session) is None:
                return render(request, "preview_waking.html", {
                    "branch": branch, "project": branch.project, "build": stale, "stale": True,
                    "need_login": True, "here": str(request.url)}, status_code=503)
            started = waking.request_wake(s, branch, target)
            return render(request, "preview_waking.html", {
                "branch": branch, "project": branch.project, "build": target, "stale": bool(stale),
                "need_login": False, "started": started}, status_code=503,
                headers={"Retry-After": "5", "Cache-Control": "no-store"})
        return render(request, "preview_status.html", {
            "branch": branch, "project": branch.project,
            "build": branch.latest_build, "live": branch.live_build,
        }, status_code=503)
    finally:
        s.close()


app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="previewer_session",
    same_site="lax",
    https_only=settings.tls_enabled,
    domain=None,  # host-only on purpose: previews get a separate pass (routes/forward_auth.py)
)


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc):
    if request.headers.get("hx-request"):
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={nxt}", status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden(request: Request, exc):
    return render(request, "error.html", {"title": "Not allowed",
                  "message": "Your role does not allow this action."}, status_code=403)


@app.exception_handler(CSRFError)
async def _csrf(request: Request, exc):
    return HTMLResponse("Invalid or missing CSRF token. Reload the page and try again.",
                        status_code=403)


@app.exception_handler(404)
async def _not_found(request: Request, exc):
    if request.url.path.startswith("/api/") or request.headers.get("hx-request"):
        return HTMLResponse("Not found", status_code=404)
    return render(request, "error.html", {"title": "Not found",
                  "message": "That page or item does not exist."}, status_code=404)


@app.get("/static/app.css", include_in_schema=False)
def app_css():
    path = Path(os.environ.get("CSS_PATH", "/app/build/app.css"))
    if not path.exists():
        return Response("", media_type="text/css")
    return FileResponse(path, media_type="text/css")


app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

for module in (health, auth, board, projects, combos, plain, dependencies, modules, branches,
               builds, accounts, admin):
    app.include_router(module.router)
app.include_router(accounts.alias_router)
app.include_router(forward_auth.router)
app.include_router(webhooks.router)
app.include_router(access.router)
# Last: /{version}/{project}/{branch} must not shadow anything above.
app.include_router(friendly.router)
