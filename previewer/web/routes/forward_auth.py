"""Traefik forward-auth for previews: every request to a preview asks here first.

Allowed (204) when the preview's project is a public demo (preview_auth_required off) or the
request carries a valid *preview pass*; otherwise a redirect to /auth/preview-pass on the
dashboard, which (after login if needed) sets the pass and sends the person back.

The pass is a separate signed cookie on .<domain>, valid only here. The dashboard session cookie
stays host-only, so branch code running in a preview never sees it and cannot act on the
dashboard. Odoo loads dozens of assets per page, so host and user lookups are cached briefly.
"""
import time
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select

from ...config import get_settings
from ...db import SessionLocal
from ...models import Branch, User

router = APIRouter()
PASS_COOKIE = "previewer_pass"
PASS_SECONDS = 12 * 3600
CACHE_SECONDS = 30
_hosts: dict[str, tuple[float, bool | None]] = {}   # host -> (when, auth required; None = unknown)
_users: dict[int, tuple[float, bool]] = {}           # user id -> (when, active)


def clear_cache() -> None:
    _hosts.clear()
    _users.clear()


def _fresh(entry) -> bool:
    return entry is not None and time.monotonic() - entry[0] < CACHE_SECONDS


def auth_required(host: str) -> bool | None:
    hit = _hosts.get(host)
    if _fresh(hit):
        return hit[1]
    s = SessionLocal()
    try:
        branch = s.scalars(select(Branch).where(Branch.host == host)).first()
        value = None if branch is None else bool(branch.project.preview_auth_required)
    finally:
        s.close()
    _hosts[host] = (time.monotonic(), value)
    return value


def user_active(user_id) -> bool:
    if not isinstance(user_id, int):
        return False
    hit = _users.get(user_id)
    if _fresh(hit):
        return hit[1]
    s = SessionLocal()
    try:
        u = s.get(User, user_id)
        value = bool(u and u.active)
    finally:
        s.close()
    _users[user_id] = (time.monotonic(), value)
    return value


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="preview-pass")


def pass_user(request: Request) -> int | None:
    token = request.cookies.get(PASS_COOKIE)
    if not token:
        return None
    try:
        data = _signer().loads(token, max_age=PASS_SECONDS)
    except BadSignature:
        return None
    return data.get("u") if isinstance(data, dict) else None


def _is_preview_url(url: str) -> bool:
    parts = urlsplit(url)
    domain = get_settings().domain.lower()
    return parts.scheme in ("http", "https") and (parts.hostname or "").lower().endswith("." + domain)


def original_url(request: Request, host: str) -> str:
    proto = request.headers.get("x-forwarded-proto") or get_settings().scheme
    uri = request.headers.get("x-forwarded-uri") or "/"
    return f"{proto}://{host}{uri}"


@router.get("/auth/forward")
def forward_auth(request: Request) -> Response:
    host = (request.headers.get("x-forwarded-host") or "").split(":")[0].lower()
    required = auth_required(host) if host else None
    if not required or user_active(pass_user(request)):
        return Response(status_code=204)
    target = f"{get_settings().dashboard_url}/auth/preview-pass?next={quote(original_url(request, host), safe='')}"
    return RedirectResponse(target, status_code=302)


@router.get("/auth/preview-pass")
def preview_pass(request: Request, next: str = "") -> Response:
    """Dashboard side: a logged-in user gets a preview pass and goes back to the preview."""
    if not _is_preview_url(next):
        return RedirectResponse("/", status_code=303)
    if not user_active(request.session.get("user_id")):
        back = f"/auth/preview-pass?next={quote(next, safe='')}"
        return RedirectResponse(f"/login?next={quote(back, safe='')}", status_code=303)
    c = get_settings()
    response = RedirectResponse(next, status_code=303)
    response.set_cookie(PASS_COOKIE, _signer().dumps({"u": request.session["user_id"]}),
                        max_age=PASS_SECONDS, domain=c.domain, httponly=True, samesite="lax",
                        secure=c.https)
    return response
