"""Runbot-style board: one row per branch, its most recent builds as cards."""
import re
from collections import Counter
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ...db import get_db
from ...models import Branch, Build, Project, User
from ..deps import paginate_list, render, tester

router = APIRouter()

SHOW = ("all", "sticky", "dev")
CARDS = 4
VERSIONISH = re.compile(r"^v?(\d+)(?:\.(\d+))?$")

# status -> icon, card border, card header, small status square (SPEC status colours)
CARD_STYLES = {
    "queued": ("hourglass", "border-gray-300", "bg-gray-100 text-gray-700", "bg-gray-400"),
    "building": ("spinner", "border-blue-300", "bg-blue-50 text-blue-800", "bg-blue-500"),
    "running": ("check", "border-green-300", "bg-green-50 text-green-800", "bg-green-500"),
    "sleeping": ("moon", "border-purple-300", "bg-purple-50 text-purple-800", "bg-purple-500"),
    "failed": ("x", "border-red-400", "bg-red-50 text-red-800", "bg-red-500"),
    "stopped": ("stop", "border-gray-600", "bg-gray-700 text-white", "bg-gray-700"),
    "removed": ("minus", "border-gray-200", "bg-gray-50 text-gray-400", "bg-gray-300"),
}


def card_style(status: str) -> dict:
    icon, border, header, square = CARD_STYLES.get(status, CARD_STYLES["removed"])
    return {"icon": icon, "border": border, "header": header, "square": square}


def superseded(build: Build) -> bool:
    """Queued builds replaced by a newer one never ran; the board leaves them out."""
    return build.status == "removed" and build.started_at is None


def board_cards(branch: Branch) -> list[Build]:
    return [b for b in branch.builds if not superseded(b)][:CARDS]


def row_key(b: Branch) -> tuple:
    """Sticky first (version-like names newest first, then main/master/others), then dev
    branches by most recent commit."""
    if b.sticky:
        m = VERSIONISH.match(b.name)
        if m:
            return (0, 0, -int(m[1]), -int(m[2] or 0), b.name, b.project.name)
        return (0, 1, 0, 0, b.name, b.project.name)
    ts = b.last_commit_at.timestamp() if b.last_commit_at else 0
    return (1, 0, -ts, 0, b.name, b.project.name)


COMBINED = "combined"  # board tab listing every combined and plain Odoo preview


def selected_project(request: Request, s: Session, project: str | None) -> Project | None:
    """`?project=<slug>|all|combined` picks the tab and is remembered in the session."""
    if project is not None:
        request.session["board_project"] = project or "all"
    slug = request.session.get("board_project", "all")
    if slug in ("all", COMBINED):
        return None
    p = s.scalars(select(Project).where(Project.slug == slug)).first()
    if p is None:
        request.session["board_project"] = "all"
    return p


def board_ctx(request: Request, s: Session, project: str | None, show: str, q: str, page: int) -> dict:
    show = show if show in SHOW else "all"
    q = q.strip()
    current = selected_project(request, s, project)
    combined = current is None and request.session.get("board_project") == COMBINED
    stmt = (select(Branch).where(Branch.deleted_at.is_(None))
            .options(selectinload(Branch.builds), selectinload(Branch.project)))
    if current:
        stmt = stmt.where(Branch.project_id == current.id)
    elif combined:
        stmt = stmt.join(Project).where(Project.kind.in_(("combo", "plain")))
    listed = list(s.scalars(stmt).all())
    scope = [b for b in listed if b.enabled]
    unselected: Counter = Counter(b.project_id for b in listed if not b.enabled)

    counts: Counter = Counter()
    cards = {}
    for b in scope:
        cards[b.id] = board_cards(b)
        for build in b.builds:
            if build.status in ("queued", "building", "running"):
                counts[build.status] += 1
        if cards[b.id] and cards[b.id][0].status == "failed":
            counts["failed"] += 1

    rows = scope
    if show == "sticky":
        rows = [b for b in rows if b.sticky]
    elif show == "dev":
        rows = [b for b in rows if not b.sticky]
    if q:
        needle = q.lower()
        rows = [b for b in rows if needle in b.name.lower() or needle in b.project.name.lower()]
    rows.sort(key=row_key)
    pg = paginate_list(rows, page)

    params = {"project": current.slug if current else (COMBINED if combined else "all"),
              "show": show, "q": q, "page": pg.page}
    all_projects = s.scalars(select(Project).order_by(Project.name)).all()
    return {
        "projects": [p for p in all_projects if not p.is_combo],
        "has_combos": any(p.is_combo for p in all_projects), "combined": combined,
        "current": current, "show": show, "q": q, "page": pg, "cards": cards,
        "counts": counts, "active": bool(counts["queued"] or counts["building"]),
        "unselected": [(p, unselected[p.id]) for p in ([current] if current else all_projects)
                       if unselected[p.id] and not p.is_combo],
        "qs": urlencode(params), "base_params": params,
    }


@router.get("/")
def board(request: Request, project: str | None = None, show: str = "all", q: str = "",
          page: int = 1, s: Session = Depends(get_db), user: User = Depends(tester)):
    return render(request, "board.html", board_ctx(request, s, project, show, q, page))


@router.get("/board/rows")
def board_rows(request: Request, project: str | None = None, show: str = "all", q: str = "",
               page: int = 1, s: Session = Depends(get_db), user: User = Depends(tester)):
    return render(request, "_board_rows.html", board_ctx(request, s, project, show, q, page))
