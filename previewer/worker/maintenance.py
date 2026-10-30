"""Phase 2 maintenance tasks: cleanup rules, Enterprise checkout and image pruning, backups.
See scheduler.Task. Each task returns a short message for the System page.

Rules (SPEC "Sleep, cleanup and resource limits"):
- a branch deleted on GitHub is removed with all its builds after deleted_branch_grace_hours;
- a branch with no new commit and no visit for stale_after_days is stopped and marked stale
  (starred branches never); once its last commit is older than stale_delete_after_days and
  nobody visited it in that time, its builds go;
- only the latest successful build per branch is kept; failed builds (except each branch's most
  recent one) and replaced builds go after failed_log_retention_days;
- Enterprise checkouts and leftover build folders nobody uses are deleted; only images this
  system built are pruned; the app database is dumped nightly.
"""
import gzip
import logging
import re
import shutil
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from .. import appsettings, jobs
from ..config import get_settings
from ..db import session_scope
from ..models import LIVE_STATUSES, Branch, Build, Job, Project, utcnow
from . import docker_ops, pg
from .logs import log_path
from .pipeline import build_dir, filestore_dir, teardown
from .scheduler import Task

log = logging.getLogger("previewer.maintenance")


def _int(key: str) -> int:
    with session_scope() as s:
        return appsettings.get_int(s, key)


def _delete_payload(b: Build) -> dict:
    return {"build_id": b.id, "container_id": b.container_id, "db_name": b.db_name, "purge_log": True}


def _cancel_queued(s, branch_id: int) -> None:
    for job in s.scalars(select(Job).where(Job.branch_id == branch_id, Job.status == "queued")):
        job.status, job.finished_at = "cancelled", utcnow()


# --- cleanup rules ------------------------------------------------------------

def cleanup_deleted() -> str:
    """Branches gone from GitHub for longer than the grace period: remove everything."""
    cutoff = utcnow() - timedelta(hours=_int("deleted_branch_grace_hours"))
    removed = []
    with session_scope() as s:
        gone = s.scalars(select(Branch).join(Project).where(
            Project.kind == "repo", Branch.deleted_at.is_not(None), Branch.deleted_at < cutoff)).all()
        for br in gone:
            if any(b.status == "building" for b in br.builds):
                continue  # finish first; next run removes it
            _cancel_queued(s, br.id)
            for b in br.builds:
                jobs.enqueue(s, "delete", _delete_payload(b))
            removed.append(f"{br.project.slug}/{br.name}")
            s.delete(br)
    return f"removed {len(removed)} deleted branch(es)" + (f": {', '.join(removed)}" if removed else "")


def _quiet_since(b: Build | None, cutoff) -> bool:
    """No visit since cutoff: last_accessed_at older (or, never visited, finished_at older)."""
    if b is None:
        return True
    seen = b.last_accessed_at or b.finished_at
    return seen is None or seen < cutoff


def cleanup_stale() -> str:
    """Stop branches without new commits or visits; delete them later unless someone visits.
    Starred (sticky) branches, e.g. long-lived version branches, are never stale."""
    now = utcnow()
    stale_cut = now - timedelta(days=_int("stale_after_days"))
    delete_cut = now - timedelta(days=_int("stale_delete_after_days"))
    stopped, cleared, deleted = [], [], []
    with session_scope() as s:
        branches = s.scalars(select(Branch).join(Project).where(
            Project.kind == "repo", Branch.deleted_at.is_(None), Branch.enabled.is_(True))).all()
        for br in branches:
            live = br.live_build or next((b for b in br.builds if b.status == "stopped"), None)
            if br.stale_since is not None:
                visited = live is not None and live.last_accessed_at and live.last_accessed_at > br.stale_since
                new_commit = br.last_commit_at and br.last_commit_at > br.stale_since
                woken = live is not None and live.status == "running"
                if br.sticky or visited or new_commit or woken:
                    br.stale_since = None
                    cleared.append(br.name)
                    continue
                if (br.last_commit_at and br.last_commit_at < delete_cut
                        and _quiet_since(live, delete_cut)):
                    if any(b.status == "building" for b in br.builds):
                        continue
                    _cancel_queued(s, br.id)
                    for b in list(br.builds):
                        jobs.enqueue(s, "delete", _delete_payload(b), branch_id=br.id)
                        s.delete(b)
                    br.enabled = False
                    br.stale_since = None
                    deleted.append(f"{br.project.slug}/{br.name}")
                continue
            if (live is not None and not br.sticky and br.last_commit_at and br.last_commit_at < stale_cut
                    and _quiet_since(live, stale_cut)):
                if live.status in LIVE_STATUSES:
                    jobs.enqueue(s, "stop", {"build_id": live.id}, branch_id=br.id, build_id=live.id)
                br.stale_since = now
                stopped.append(f"{br.project.slug}/{br.name}")
    return (f"stopped {len(stopped)} stale, cleared {len(cleared)}, deleted {len(deleted)}"
            + (f" (stopped: {', '.join(stopped)})" if stopped else "")
            + (f" (deleted: {', '.join(deleted)})" if deleted else ""))


def cleanup_builds() -> str:
    """Leftovers of replaced builds, and old failed / replaced build rows with their logs."""
    cutoff = utcnow() - timedelta(days=_int("failed_log_retention_days"))
    c = get_settings()
    on_disk = {int(p.name) for d in (c.builds_dir, c.filestore_dir) if d.exists()
               for p in d.iterdir() if p.name.isdigit()}
    try:
        containers = {int(x.labels["previewer.build_id"]) for x in docker_ops.preview_containers()}
    except Exception:  # noqa: BLE001 — Docker down: skip the leftover check this time
        containers = set()
    purged, dropped = 0, 0
    with session_scope() as s:
        removed = s.scalars(select(Build).where(Build.status == "removed")).all()
        leftovers = [(b.id, b.db_name) for b in removed if b.id in on_disk or b.id in containers
                     or (b.db_name and b.finished_at and b.finished_at > utcnow() - timedelta(days=7))]
        latest_failed = {}
        for b in s.scalars(select(Build).where(Build.status == "failed").order_by(Build.id)):
            latest_failed[b.branch_id] = b.id
        old = s.scalars(select(Build).where(
            Build.status.in_(("failed", "removed")),
            Build.created_at < cutoff)).all()
        doomed = [b for b in old if not (b.status == "failed" and latest_failed.get(b.branch_id) == b.id)]
        for b in doomed:
            log_path(b.id).unlink(missing_ok=True)
            s.delete(b)
        dropped = len(doomed)
    for build_id, db_name in leftovers:
        if build_id in on_disk or build_id in containers or (db_name and pg.database_exists(db_name)):
            teardown(build_id, db_name, keep_log=True)
            purged += 1
    return f"cleaned leftovers of {purged} replaced build(s), dropped {dropped} old build record(s)"


# --- pruning --------------------------------------------------------------------

_CHECKOUT = re.compile(r"^(enterprise-.+)-([0-9a-f]{12})(\.tmp)?$")


def prune_enterprise(dry_run: bool = False) -> str:
    """Enterprise checkouts no container mounts (keeping the newest per repo+branch), build and
    filestore folders and logs whose build row is gone, and orphaned preview containers."""
    c = get_settings()
    containers = docker_ops.preview_containers()
    mounted = {Path(p).name for p in docker_ops.mounted_sources(containers)}
    newest: dict[str, Path] = {}
    for p in gitmirror_checkouts():
        m = _CHECKOUT.match(p.name)
        if m and not m.group(3) and (p / ".complete").exists():
            key = m.group(1)
            if key not in newest or p.stat().st_mtime > newest[key].stat().st_mtime:
                newest[key] = p
    doomed: list[Path] = []
    for p in gitmirror_checkouts():
        if p.name in mounted or p in newest.values():
            continue
        if p.stat().st_mtime > (utcnow() - timedelta(hours=6)).timestamp():
            continue  # being exported, or exported for a build that has not started its container yet
        doomed.append(p)

    with session_scope() as s:
        known = set(s.scalars(select(Build.id)))
    for d in (c.builds_dir, c.filestore_dir):
        if d.exists():
            doomed += [p for p in d.iterdir() if p.name.isdigit() and int(p.name) not in known]
    orphan_logs = [p for p in c.logs_dir.glob("*.log") if p.stem.isdigit() and int(p.stem) not in known] \
        if c.logs_dir.exists() else []
    orphan_ctrs = [x for x in containers if int(x.labels.get("previewer.build_id", "0")) not in known]

    freed = sum(_size(p) for p in doomed)
    names = [str(p.relative_to(c.data_dir)) for p in doomed] + [x.name for x in orphan_ctrs]
    if dry_run:
        return f"would delete {len(names)} item(s), {freed / 1e6:.0f} MB: {', '.join(names) or 'nothing'}"
    for p in doomed:
        shutil.rmtree(p, ignore_errors=True)
    for p in orphan_logs:
        p.unlink(missing_ok=True)
    for x in orphan_ctrs:
        x.remove(force=True)
    return f"deleted {len(names)} item(s), freed {freed / 1e6:.0f} MB" + (f": {', '.join(names)}" if names else "")


def gitmirror_checkouts() -> list[Path]:
    from ..gitmirror import enterprise_checkouts
    return enterprise_checkouts()


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())


def prune_images() -> str:
    """Images this system built (previewer-odoo:*) that no container uses, and dangling ones
    carrying our label. Other stacks' images are never touched."""
    dc = docker_ops.client()
    used = {x.attrs.get("Image") for x in dc.containers.list(all=True)}
    freed, removed = 0, []
    for img in dc.images.list():
        tags = img.tags or []
        ours = any(t.startswith("previewer-odoo") for t in tags) or \
            (img.labels or {}).get(docker_ops.IMAGE_LABEL) == "1"
        if not ours or img.id in used:
            continue
        size = img.attrs.get("Size", 0)
        try:
            dc.images.remove(img.id, force=False)
        except Exception as e:  # noqa: BLE001 — e.g. a child image still uses it
            log.info("kept image %s: %s", tags or img.id, e)
            continue
        freed += size
        removed.append(",".join(tags) or img.short_id)
    res = dc.images.prune(filters={"dangling": True, "label": f"{docker_ops.IMAGE_LABEL}=1"})
    freed += (res or {}).get("SpaceReclaimed") or 0
    return f"removed {len(removed)} image(s), reclaimed {freed / 1e6:.0f} MB"


# --- backups ----------------------------------------------------------------------

def backup_dir() -> Path:
    return get_settings().data_dir / "backups"


def backup() -> str:
    """pg_dump of the app database, run inside the postgres container, gzipped to
    DATA_DIR/backups/previewer-YYYY-MM-DD.sql.gz; older than backup_keep_days are deleted."""
    c = get_settings()
    dc = docker_ops.client()
    pgs = dc.containers.list(filters={"label": "com.docker.compose.service=postgres",
                                      "network": c.docker_network})
    if not pgs:
        raise RuntimeError("postgres container not found on the previewer network")
    out_dir = backup_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"previewer-{utcnow():%Y-%m-%d}.sql.gz"
    tmp = dest.with_suffix(".tmp")
    exec_id = dc.api.exec_create(pgs[0].id, ["pg_dump", "-U", c.postgres_user, "--no-owner", c.postgres_db])["Id"]
    errors = b""
    with gzip.open(tmp, "wb") as fh:
        for out, err in dc.api.exec_start(exec_id, stream=True, demux=True):
            if out:
                fh.write(out)
            if err:
                errors += err
    code = dc.api.exec_inspect(exec_id).get("ExitCode")
    if code != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump exited {code}: {errors.decode(errors='replace')[-300:]}")
    tmp.replace(dest)
    keep = timedelta(days=_int("backup_keep_days"))
    old = [p for p in out_dir.glob("previewer-*.sql.gz")
           if p != dest and utcnow().timestamp() - p.stat().st_mtime > keep.total_seconds()]
    for p in old:
        p.unlink(missing_ok=True)
    return f"{dest.name} ({dest.stat().st_size / 1e3:.0f} kB), removed {len(old)} old backup(s)"


TASKS: list[Task] = [
    Task("cleanup_deleted", lambda: timedelta(hours=1), cleanup_deleted),
    Task("cleanup_stale", lambda: timedelta(days=1), cleanup_stale),
    Task("cleanup_builds", lambda: timedelta(hours=1), cleanup_builds),
    Task("prune_enterprise", lambda: timedelta(days=1), prune_enterprise),
    Task("prune_images", lambda: timedelta(days=max(1, _int("image_prune_days"))), prune_images),
    Task("backup", lambda: timedelta(days=1), backup),
]
