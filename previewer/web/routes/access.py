"""Per-project preview access (login required or public demo), GitHub commit statuses and
the GitHub webhook secret. Shared by repo projects, combined and plain previews."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import Project, User
from ...security import random_secret
from ..deps import admin, flash
from . import forward_auth

router = APIRouter()


def project_page(p: Project) -> str:
    return f"/combos/{p.id}?tab=settings" if p.is_combo else f"/projects/{p.id}?tab=settings"


def _get(s: Session, pid: int) -> Project:
    p = s.get(Project, pid)
    if not p:
        raise HTTPException(404)
    return p


@router.post("/projects/{pid}/access")
async def save_access(pid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(admin)):
    p = _get(s, pid)
    form = await request.form()
    p.preview_auth_required = form.get("preview_auth_required") in ("on", "1", "true")
    if not p.is_plain:
        p.post_commit_status = form.get("post_commit_status") in ("on", "1", "true")
    s.commit()
    forward_auth.clear_cache()
    flash(request, "Preview access saved: " + ("a dashboard login is needed to open the previews."
                   if p.preview_auth_required else "anyone with the link can open the previews."), "success")
    return RedirectResponse(project_page(p), status_code=303)


@router.get("/projects/{pid}/webhook-secret", response_class=HTMLResponse)
def reveal_secret(pid: int, s: Session = Depends(get_db), user: User = Depends(admin)):
    p = _get(s, pid)
    if not p.webhook_secret:
        return '<span class="text-amber-700">not set: press Regenerate</span>'
    return f'<code class="select-all break-all rounded bg-gray-100 px-1">{escape(p.webhook_secret)}</code>'


@router.post("/projects/{pid}/webhook-secret")
def regenerate_secret(pid: int, request: Request, s: Session = Depends(get_db), user: User = Depends(admin)):
    p = _get(s, pid)
    p.webhook_secret = random_secret(32)
    s.commit()
    flash(request, "New webhook secret created. Paste it into the webhook on GitHub; the old one stops working.",
          "success")
    return RedirectResponse(project_page(p), status_code=303)
