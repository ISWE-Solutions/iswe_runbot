from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db import get_db
from ...models import User
from ...security import verify_password
from ..deps import render

router = APIRouter()


def _safe_next(nxt: str | None) -> str:
    """A dashboard path, or an absolute URL on the dashboard or a preview host (forward-auth
    sends people back to the preview they opened). Anything else would be an open redirect."""
    if not nxt:
        return "/"
    if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt:
        return nxt
    parts = urlsplit(nxt)
    domain = get_settings().domain.lower()
    host = (parts.hostname or "").lower()
    if parts.scheme in ("http", "https") and (host == domain or host.endswith("." + domain)):
        return nxt
    return "/"


@router.get("/login")
def login_form(request: Request, next: str | None = None, s: Session = Depends(get_db)):
    uid = request.session.get("user_id")
    user = s.get(User, uid) if isinstance(uid, int) else None
    if user and user.active and next:
        return RedirectResponse(_safe_next(next), status_code=303)
    return render(request, "login.html", {"next": _safe_next(next)})


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...),
          next: str = Form("/"), s: Session = Depends(get_db)):
    user = s.scalars(select(User).where(User.email == email.strip().lower())).first()
    if not user or not user.active or not verify_password(password, user.password_hash):
        return render(request, "login.html", {"next": _safe_next(next), "email": email,
                      "error": "Wrong email or password."}, status_code=400)
    csrf = request.session.get("csrf")
    request.session.clear()
    request.session.update({"user_id": user.id, "csrf": csrf})
    return RedirectResponse(_safe_next(next), status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("previewer_pass", domain=get_settings().domain)  # forward_auth.PASS_COOKIE
    return response
