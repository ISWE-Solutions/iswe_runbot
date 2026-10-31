import logging
import secrets
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db import get_db
from ...models import User
from ...security import verify_password
from .. import sso
from ..deps import render

router = APIRouter()
log = logging.getLogger(__name__)
SSO_SESSION_KEY = "sso"       # {provider, state, nonce, next} while the browser is at the provider
LOGIN_ERROR_KEY = "login_error"


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


def _login_page(request: Request, ctx: dict, status_code: int = 200):
    c = get_settings()
    ctx = {"microsoft_ready": sso.configured("microsoft", c), "github_ready": sso.configured("github", c),
           "sso_missing": sso.not_set_up_message(c), **ctx}
    return render(request, "login.html", ctx, status_code=status_code)


def _sign_in(request: Request, user: User, nxt: str | None) -> RedirectResponse:
    csrf = request.session.get("csrf")
    request.session.clear()
    request.session.update({"user_id": user.id, "csrf": csrf})
    return RedirectResponse(_safe_next(nxt), status_code=303)


@router.get("/login")
def login_form(request: Request, next: str | None = None, s: Session = Depends(get_db)):
    uid = request.session.get("user_id")
    user = s.get(User, uid) if isinstance(uid, int) else None
    if user and user.active and next:
        return RedirectResponse(_safe_next(next), status_code=303)
    return _login_page(request, {"next": _safe_next(next),
                                 "sso_error": request.session.pop(LOGIN_ERROR_KEY, None)})


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...),
          next: str = Form("/"), s: Session = Depends(get_db)):
    user = s.scalars(select(User).where(User.email == email.strip().lower())).first()
    if not user or not user.active or not verify_password(password, user.password_hash):
        return _login_page(request, {"next": _safe_next(next), "email": email,
                           "error": "Wrong email, username or password."}, status_code=400)
    return _sign_in(request, user, next)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("previewer_pass", domain=get_settings().domain)  # forward_auth.PASS_COOKIE
    return response


# ---------- Sign in with Microsoft / GitHub (web/sso.py) ----------

def _back_to_login(request: Request, message: str, nxt: str | None = None) -> RedirectResponse:
    request.session[LOGIN_ERROR_KEY] = message
    nxt = _safe_next(nxt)
    return RedirectResponse("/login" if nxt == "/" else f"/login?next={quote(nxt, safe='')}",
                            status_code=303)


def _start(request: Request, provider: str, nxt: str | None) -> RedirectResponse:
    if not sso.configured(provider):
        return _back_to_login(request, f"{sso.LABELS[provider]} sign-in is not set up yet.", nxt)
    state, nonce = sso.new_state(), sso.new_state()
    request.session[SSO_SESSION_KEY] = {"provider": provider, "state": state, "nonce": nonce,
                                        "next": _safe_next(nxt)}
    url = (sso.microsoft_authorize_url(state, nonce) if provider == "microsoft"
           else sso.github_authorize_url(state))
    return RedirectResponse(url, status_code=303)


def _finish(request: Request, s: Session, provider: str, code: str | None, state: str | None,
            error: str | None) -> RedirectResponse:
    label = sso.LABELS[provider]
    pending = request.session.pop(SSO_SESSION_KEY, None)
    pending = pending if isinstance(pending, dict) and pending.get("provider") == provider else None
    nxt = pending.get("next") if pending else None
    if not sso.configured(provider):
        return _back_to_login(request, f"{label} sign-in is not set up yet.", nxt)
    if error:
        log.warning("%s sign-in: the provider returned %s", label, error[:100])
        return _back_to_login(request, f"{label} sign-in was cancelled or failed ({error[:100]}).", nxt)
    if not pending or not state or not secrets.compare_digest(state, str(pending.get("state", ""))):
        log.warning("%s sign-in: state missing or does not match the session", label)
        return _back_to_login(request, f"That {label} sign-in expired or was started in another "
                                       "window. Please try again.", nxt)
    if not code:
        return _back_to_login(request, f"{label} did not return a sign-in code. Please try again.", nxt)
    try:
        who = (sso.microsoft_identity(code, str(pending.get("nonce", ""))) if provider == "microsoft"
               else sso.github_identity(code))
    except sso.SSOError as e:
        log.warning("%s sign-in refused: %s", label, e)
        return _back_to_login(request, str(e), nxt)

    user = s.scalars(select(User).where(func.lower(User.email) == who.email)).first()
    if user is None:
        user = User(email=who.email, name=who.name or who.email.split("@")[0], password_hash="")
        s.add(user)
        s.commit()
        log.info("%s sign-in created user %s", label, who.email)
    if not user.active:
        log.warning("%s sign-in refused for disabled user %s", label, who.email)
        return _back_to_login(request, "Your account is disabled.", nxt)
    return _sign_in(request, user, nxt)


@router.get("/auth/microsoft")
def microsoft_start(request: Request, next: str | None = None):
    return _start(request, "microsoft", next)


@router.get("/auth/microsoft/callback")
def microsoft_callback(request: Request, code: str | None = None, state: str | None = None,
                       error: str | None = None, s: Session = Depends(get_db)):
    return _finish(request, s, "microsoft", code, state, error)


@router.get("/auth/github")
def github_start(request: Request, next: str | None = None):
    return _start(request, "github", next)


@router.get("/auth/github/callback")
def github_callback(request: Request, code: str | None = None, state: str | None = None,
                    error: str | None = None, s: Session = Depends(get_db)):
    return _finish(request, s, "github", code, state, error)
