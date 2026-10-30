from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import jobs
from ...config import get_settings
from ...db import get_db
from ...models import Build, Job, User, utcnow
from ..deps import ACTIVE_STATUSES, developer, flash, render, tester
from .branches import _done

router = APIRouter()

CHUNK = 64 * 1024
INITIAL_TAIL = 256 * 1024


def get_build(s: Session, build_id: int) -> Build:
    b = s.get(Build, build_id)
    if not b:
        raise HTTPException(404)
    return b


def log_file(build: Build) -> Path:
    """The build's log, constrained to DATA_DIR/logs whatever log_path says."""
    logs = get_settings().logs_dir.resolve()
    path = Path(build.log_path) if build.log_path else logs / f"{build.id}.log"
    path = path.resolve()
    if logs not in path.parents:
        path = logs / f"{build.id}.log"
    return path


def _complete_utf8(data: bytes) -> bytes:
    """Drop a trailing partial UTF-8 sequence; the next poll re-reads it."""
    for i in range(1, min(4, len(data)) + 1):
        b = data[-i]
        if b & 0xC0 == 0x80:
            continue
        need = 1 if b < 0x80 else 2 if b < 0xE0 else 3 if b < 0xF0 else 4
        return data if need <= i else data[:-i]
    return data


def still_writing(build: Build) -> bool:
    return build.status in ACTIVE_STATUSES or build.status == "running"


@router.get("/builds/{build_id}")
def build_page(build_id: int, request: Request, tab: str = "", s: Session = Depends(get_db),
               user: User = Depends(tester)):
    build = get_build(s, build_id)
    path = log_file(build)
    size = path.stat().st_size if path.exists() else 0
    failed = build.status == "failed"
    tabs = (["why"] if failed else []) + ["plan", "log", "details"]
    return render(request, "build.html", {
        "build": build, "branch": build.branch, "project": build.branch.project,
        "start_offset": max(0, size - INITIAL_TAIL), "truncated": size > INITIAL_TAIL,
        "tab": tab if tab in tabs else ("why" if failed else "log"),
    })


@router.get("/builds/{build_id}/log")
def log_chunk(build_id: int, request: Request, offset: int = 0, s: Session = Depends(get_db),
              user: User = Depends(tester)):
    """Appends new bytes to #log (out-of-band) and returns the next poller."""
    build = get_build(s, build_id)
    path = log_file(build)
    data = b""
    if path.exists():
        with path.open("rb") as f:
            f.seek(max(0, offset))
            data = f.read(CHUNK)
        data = _complete_utf8(data)
    new_offset = offset + len(data)
    more = len(data) == CHUNK or still_writing(build)
    text = escape(data.decode("utf-8", errors="replace"))
    poller = ""
    if more:
        delay = "0.2s" if len(data) == CHUNK else "2s"
        poller = (f'<div id="log-poller" hx-get="/builds/{build.id}/log?offset={new_offset}" '
                  f'hx-trigger="load delay:{delay}" hx-swap="outerHTML"></div>')
    else:
        poller = '<div id="log-poller" data-done="1"></div>'
    status = (f'<span id="build-status" hx-swap-oob="true" class="inline-flex">'
              f'{render_status(build)}</span>')
    append = f'<div hx-swap-oob="beforeend:#log">{text}</div>' if text else ""
    return HTMLResponse(poller + append + status)


def render_status(build: Build) -> str:
    from ..deps import STATUS_CLASSES
    cls = STATUS_CLASSES.get(build.status, STATUS_CLASSES["none"])
    return f'<span class="rounded-full px-2.5 py-0.5 text-xs font-medium {cls}">{escape(build.status)}</span>'


@router.get("/builds/{build_id}/log/download")
def download_log(build_id: int, s: Session = Depends(get_db), user: User = Depends(tester)):
    build = get_build(s, build_id)
    path = log_file(build)
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="text/plain", filename=f"build-{build.id}.log")


# ---------- delete ----------

# Statuses whose container serves (or can serve again) the branch preview
PREVIEW_STATUSES = ("running", "sleeping", "stopped")


def remove_build(s: Session, build: Build) -> str:
    """Delete one build. Returns 'building' (refused), 'cancelled' (was queued), 'live'
    (its preview is gone) or 'deleted'. The worker removes the container, database, files
    and log from the snapshot payload, since the row is deleted here. Caller commits."""
    if build.status == "building":
        return "building"
    for job in s.scalars(select(Job).where(Job.build_id == build.id, Job.status == "queued")):
        job.status, job.finished_at = "cancelled", utcnow()
    outcome = "cancelled" if build.status == "queued" else \
        "live" if build.status in PREVIEW_STATUSES else "deleted"
    if outcome != "cancelled":
        jobs.enqueue(s, "delete", {"build_id": build.id, "container_id": build.container_id,
                                   "db_name": build.db_name, "purge_log": True},
                     branch_id=build.branch_id)
    s.delete(build)
    s.flush()
    return outcome


def flash_removed(request: Request, branch, build_id: int, outcome: str) -> None:
    if outcome == "building":
        flash(request, f"Build #{build_id} is still building; delete it when it finishes.", "error")
    elif outcome == "cancelled":
        flash(request, f"Queued build #{build_id} cancelled and deleted.", "info")
    elif outcome == "live":
        until = "the next new commit" if branch.auto_rebuild else "you press Rebuild"
        flash(request, f"Build #{build_id} deleted: the {branch.name} preview is gone. "
                       f"It will not rebuild until {until}.", "info")
    else:
        flash(request, f"Build #{build_id} deleted.", "info")


@router.post("/builds/{build_id}/delete")
def delete_build(build_id: int, request: Request, s: Session = Depends(get_db),
                 user: User = Depends(developer)):
    build = get_build(s, build_id)
    branch = build.branch
    outcome = remove_build(s, build)
    flash_removed(request, branch, build_id, outcome)
    s.commit()
    s.refresh(branch)
    if request.headers.get("hx-request") or outcome == "building":
        return _done(request, branch, s)
    return RedirectResponse(f"/branches/{branch.id}", status_code=303)
