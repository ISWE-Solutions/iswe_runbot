"""Friendly links: /<version>/<project>/<branch> (or /<version>/<combo>) -> 302 to the
preview's own host."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import COMBO_BRANCH, Branch, Project, User
from ..deps import render, tester
from ..views import detail_url, friendly_path, preview_url

router = APIRouter()


@router.get("/{version:odoover}/{project_slug}")
def friendly_combo(version: str, project_slug: str, request: Request,
                   s: Session = Depends(get_db), user: User = Depends(tester)):
    """/<version>/<combo-slug>: a combined preview has no branch segment."""
    project = s.scalars(select(Project).where(Project.slug == project_slug)).first()
    if not project:
        raise HTTPException(404)
    if not project.is_combo:
        return RedirectResponse(f"/projects/{project.id}", status_code=302)
    b = next((x for x in project.branches if x.name == COMBO_BRANCH), None)
    if not b:
        raise HTTPException(404)
    return _go(request, version, project, b)


@router.get("/{version:odoover}/{project_slug}/{branch:path}")
def friendly(version: str, project_slug: str, branch: str, request: Request,
             s: Session = Depends(get_db), user: User = Depends(tester)):
    project = s.scalars(select(Project).where(Project.slug == project_slug)).first()
    if not project:
        raise HTTPException(404)
    b = s.scalars(select(Branch).where(
        Branch.project_id == project.id,
        or_(Branch.slug == branch.strip("/"), Branch.name == branch.strip("/")),
    ).order_by(Branch.deleted_at.is_not(None))).first()
    if not b:
        raise HTTPException(404)
    return _go(request, version, project, b)


def _go(request: Request, version: str, project: Project, b: Branch):
    live = b.live_build
    current = live.odoo_version if live else b.effective_version
    if version != current:
        return render(request, "friendly_mismatch.html", {
            "branch": b, "project": project, "asked": version, "current": current,
            "link": friendly_path(b),
        }, status_code=404)
    if live and live.status == "running" and b.host:
        return RedirectResponse(preview_url(b), status_code=302)
    return RedirectResponse(detail_url(b), status_code=302)
