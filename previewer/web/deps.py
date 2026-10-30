"""Shared web helpers: templates, auth/roles, CSRF, flash messages, pagination."""
import math
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import User

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

ROLE_RANK = {"tester": 0, "developer": 1, "admin": 2}

STATUS_CLASSES = {
    "queued": "bg-gray-200 text-gray-700",
    "building": "bg-blue-100 text-blue-800",
    "running": "bg-green-100 text-green-800",
    "sleeping": "bg-purple-100 text-purple-800",
    "failed": "bg-red-100 text-red-800",
    "stopped": "bg-gray-700 text-white",
    "removed": "bg-gray-100 text-gray-400",
    "none": "bg-gray-50 text-gray-400 ring-1 ring-gray-200",
}
ACTIVE_STATUSES = ("queued", "building")
PER_PAGE = 10


class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    pass


class CSRFError(Exception):
    pass


# ---------- templating ----------

def ago(dt: datetime | None) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 0:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit} ago"
    return "just now"


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


def flash(request: Request, message: str, kind: str = "info") -> None:
    request.session.setdefault("flashes", []).append({"kind": kind, "message": message})


def pop_flashes(request: Request) -> list[dict]:
    return request.session.pop("flashes", [])


def can(user: User | None, role: str) -> bool:
    return bool(user and user.active and ROLE_RANK.get(user.role, -1) >= ROLE_RANK[role])


templates.env.filters["ago"] = ago
templates.env.globals.update(
    settings=get_settings(),
    status_class=lambda st: STATUS_CLASSES.get(st or "none", STATUS_CLASSES["none"]),
    csrf_token=csrf_token,
    pop_flashes=pop_flashes,
    can=can,
)


def render(request: Request, name: str, ctx: dict | None = None, status_code: int = 200, **kw):
    context = {"user": getattr(request.state, "user", None)}
    context.update(ctx or {})
    return templates.TemplateResponse(request, name, context, status_code=status_code, **kw)


def page_url(request: Request, _path: str | None = None, **changes) -> str:
    params = dict(request.query_params)
    params.update({k: v for k, v in changes.items() if v is not None})
    return f"{_path or request.url.path}?{urlencode(params)}"


templates.env.globals["page_url"] = page_url


# ---------- auth ----------

def current_user(request: Request, s: Session = Depends(get_db)) -> User:
    uid = request.session.get("user_id")
    user = s.get(User, uid) if uid else None
    if not user or not user.active:
        raise LoginRequired()
    request.state.user = user
    return user


def require(role: str):
    def dep(user: User = Depends(current_user)) -> User:
        if not can(user, role):
            raise Forbidden()
        return user

    return dep


tester = require("tester")
developer = require("developer")
admin = require("admin")


# ---------- CSRF (app-wide dependency) ----------

# Machine-to-machine endpoints authenticated otherwise (GitHub HMAC signature)
CSRF_EXEMPT_PREFIXES = ("/api/webhooks/",)


async def csrf_protect(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS") or request.url.path.startswith(CSRF_EXEMPT_PREFIXES):
        return
    expected = request.session.get("csrf")
    sent = request.headers.get("x-csrf-token")
    if not sent:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            sent = (await request.form()).get("csrf_token")
    if not expected or not sent or not secrets.compare_digest(str(sent), expected):
        raise CSRFError()


# ---------- pagination ----------

class Page:
    def __init__(self, items, page: int, total: int, per_page: int = PER_PAGE):
        self.items, self.page, self.total, self.per_page = items, page, total, per_page
        self.pages = max(1, math.ceil(total / per_page))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages


def paginate(s: Session, stmt, page: int, per_page: int = PER_PAGE) -> Page:
    total = s.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    page = max(1, min(page, max(1, math.ceil(total / per_page))))
    items = s.scalars(stmt.limit(per_page).offset((page - 1) * per_page)).all()
    return Page(items, page, total, per_page)


def paginate_list(items: list, page: int, per_page: int = PER_PAGE) -> Page:
    total = len(items)
    page = max(1, min(page, max(1, math.ceil(total / per_page))))
    return Page(items[(page - 1) * per_page: page * per_page], page, total, per_page)
