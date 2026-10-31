"""Phase 2 automation tasks: polling, sleep on idle, visit tracking, running-preview limit,
disk guard, resource stats. See scheduler.Task."""
import json
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from .. import appsettings, capacity, jobs
from ..config import get_settings
from ..db import session_scope
from ..models import Branch, Build, Job, Project, utcnow
from . import docker_ops, pg
from .logs import BuildLog
from .scheduler import Task

log = logging.getLogger("previewer.automation")

# Visits: on first start only the tail of an existing access log is read
FIRST_READ_BYTES = 5 * 1024 * 1024
VISIT_THROTTLE = timedelta(minutes=1)
DISK_HOLD = timedelta(minutes=5)


def _setting(key: str) -> str:
    with session_scope() as s:
        return appsettings.get(s, key)


def _cache_dir() -> Path:
    d = get_settings().data_dir / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- poll ---------------------------------------------------------------------------------

def poll() -> str:
    """Fallback for missed webhooks: one sync per project unless one is already pending."""
    with session_scope() as s:
        pending = {(j.payload or {}).get("project_id") for j in s.scalars(
            select(Job).where(Job.type == "sync", Job.status.in_(("queued", "running"))))}
        queued = 0
        for pid in s.scalars(select(Project.id).order_by(Project.id)):
            if pid not in pending:
                jobs.enqueue(s, "sync", {"project_id": pid})
                queued += 1
    return f"queued {queued} sync job(s)"


# --- visits -------------------------------------------------------------------------------

def _parse_time(value: str) -> datetime | None:
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def read_new_visits(log_path: Path, state_path: Path) -> dict[str, datetime]:
    """Newest request time per host since the last call. The byte offset (with the file's
    inode, to notice rotation) is kept in state_path; only complete lines are consumed."""
    if not log_path.exists():
        return {}
    st = log_path.stat()
    try:
        saved = json.loads(state_path.read_text())
    except (OSError, ValueError):
        saved = None
    if saved and saved.get("inode") == st.st_ino and saved.get("offset", 0) <= st.st_size:
        offset = saved["offset"]
    elif saved:
        offset = 0  # rotated or truncated: start over
    else:
        offset = max(0, st.st_size - FIRST_READ_BYTES)
    with open(log_path, "rb") as f:
        f.seek(offset)
        data = f.read()
    skip = data.find(b"\n") + 1 if offset and not saved else 0  # partial first line
    end = max(skip, data.rfind(b"\n") + 1)
    newest: dict[str, datetime] = {}
    for line in data[skip:end].splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        host = str(rec.get("RequestHost") or "").split(":")[0].lower()
        t = _parse_time(rec.get("time") or "")
        if host and t and (host not in newest or t > newest[host]):
            newest[host] = t
    state_path.write_text(json.dumps({"inode": st.st_ino, "offset": offset + end}))
    return newest


def record_visits(newest: dict[str, datetime]) -> int:
    """Set last_accessed_at on each visited host's live build (one write per build a minute)."""
    if not newest:
        return 0
    updated = 0
    with session_scope() as s:
        for branch in s.scalars(select(Branch).where(Branch.host.in_(list(newest)))):
            build = branch.live_build
            seen = newest[branch.host]
            if build and (build.last_accessed_at is None
                          or seen - build.last_accessed_at >= VISIT_THROTTLE):
                build.last_accessed_at = seen
                updated += 1
    return updated


def visits() -> str:
    newest = read_new_visits(get_settings().logs_dir / "traefik" / "access.log",
                             _cache_dir() / "access-offset")
    return f"{len(newest)} host(s) visited, {record_visits(newest)} build(s) updated"


# --- sleep and the running limit ----------------------------------------------------------

def _last_activity(b: Build) -> datetime:
    return b.last_accessed_at or b.finished_at or b.started_at or b.created_at


def sleep_idle() -> str:
    from . import lifecycle
    with session_scope() as s:
        hours = appsettings.get_int(s, "idle_sleep_hours")
        if hours <= 0:
            return "sleep disabled (idle_sleep_hours = 0)"
        cutoff = utcnow() - timedelta(hours=hours)
        idle = [b.id for b in s.scalars(select(Build).where(Build.status == "running"))
                if _last_activity(b) < cutoff]
    for build_id in idle:
        lifecycle.sleep(build_id, f"no visits for {hours} h")
    limited = enforce_running_limit()
    return f"slept {len(idle)} idle, {len(limited)} over the limit"


_host_cache: dict[str, tuple[float, int | None]] = {}
HOST_CACHE_SECONDS = 600


def _cached(key: str, fetch) -> int | None:
    hit = _host_cache.get(key)
    if hit and time.monotonic() - hit[0] < HOST_CACHE_SECONDS:
        return hit[1]
    try:
        value = fetch()
    except Exception:  # noqa: BLE001 — unknown: the setting applies as is
        value = None
    _host_cache[key] = (time.monotonic(), value)
    return value


def host_memory() -> int | None:
    return _cached("mem", lambda: int(docker_ops.client().info().get("MemTotal") or 0) or None)


def running_limit() -> int:
    """max_running_previews, capped to what fits in the Docker host's memory (next to the
    builds and the system reserve) and in Postgres' max_connections."""
    c = get_settings()
    with session_scope() as s:
        setting = appsettings.get_int(s, "max_running_previews")
        preview_mem = appsettings.get(s, "preview_memory_limit")
        build_mem = appsettings.get(s, "build_memory_limit")
        builds = appsettings.get_int(s, "max_concurrent_builds")
        overcommit = float(appsettings.get(s, "memory_overcommit") or 1)
    return capacity.effective_limit(
        max(1, setting), host_memory(), preview_mem=preview_mem, builds=max(1, builds),
        build_mem=build_mem, reserved=c.reserved_memory, overcommit=overcommit,
        maxconn=c.odoo_db_maxconn, max_connections=_cached("conn", pg.max_connections))


def _live_containers() -> dict[int, object] | None:
    try:
        return docker_ops.running_previews()
    except Exception:  # noqa: BLE001 — no Docker: count from the database alone
        return None


def enforce_running_limit(exclude: int | None = None, reserve: int = 0,
                          spare: frozenset[int] = frozenset()) -> list[int]:
    """Sleep the longest-idle running previews until at most the running limit run, counting
    every running preview container (also ones still starting) plus `reserve` about to start.
    Containers running for a build that is not running/building (e.g. a wake that failed
    before) are stopped. `exclude` and `spare` are never chosen."""
    from . import lifecycle
    limit = running_limit()
    live = _live_containers()
    with session_scope() as s:
        running = sorted(s.scalars(select(Build).where(Build.status == "running")), key=_last_activity)
        if live is None:
            count = len(running)
            orphans = []
        else:
            status = dict(s.execute(select(Build.id, Build.status).where(Build.id.in_(list(live)))).all())
            waking = {(j.payload or {}).get("build_id") for j in s.scalars(
                select(Job).where(Job.type.in_(("start", "wake")), Job.status == "running"))}
            orphans = [bid for bid in live if status.get(bid) not in ("running", "building")
                       and bid not in waking and bid != exclude]
            count = len(live) - len(orphans)
        candidates = [b.id for b in running if b.id != exclude and b.id not in spare
                      and (live is None or b.id in live)]
    for build_id in orphans:
        log.warning("stopping preview container of build %s: it runs but the build is not live", build_id)
        docker_ops.stop_container(docker_ops.container_name(build_id))
    victims = candidates[:max(0, count + reserve - limit)]
    for build_id in victims:
        lifecycle.sleep(build_id, f"more than {limit} previews running; this one was idle longest")
    return victims


def make_room(starting: int) -> list[int]:
    """Before a preview starts: sleep idle ones so it fits within the running limit. Other
    builds of the same branch are spared (the live one stays up until the new one is healthy)."""
    with session_scope() as s:
        b = s.get(Build, starting)
        siblings = frozenset(s.scalars(select(Build.id).where(Build.branch_id == b.branch_id))) if b else frozenset()
    return enforce_running_limit(exclude=starting, reserve=1, spare=siblings)


# --- disk ---------------------------------------------------------------------------------

def disk_status() -> dict:
    u = shutil.disk_usage(get_settings().data_dir)
    return {"free_gb": round(u.free / 1e9, 1), "total_gb": round(u.total / 1e9, 1),
            "percent_used": round(100 * u.used / u.total, 1) if u.total else 0}


def disk_guard() -> str:
    """Hold queued builds while free disk is below disk_min_free_gb instead of letting them
    fail half-way (image pulls and Enterprise exports need several GB)."""
    d = disk_status()
    need = float(_setting("disk_min_free_gb"))
    if d["free_gb"] >= need:
        return f"{d['free_gb']} GB free"
    held = 0
    now = utcnow()
    with session_scope() as s:
        for job in s.scalars(select(Job).where(Job.type == "build", Job.status == "queued",
                                               Job.run_after <= now)):
            job.run_after = now + DISK_HOLD
            held += 1
            if job.build_id:
                BuildLog(job.build_id).step(
                    f"Waiting for disk space: {d['free_gb']} GB free, need {need:g} GB")
    return f"low disk: {d['free_gb']} GB free (need {need:g}); held {held} build(s)"


# --- stats for the System page ------------------------------------------------------------

_prev_cpu: dict[int, tuple[int, int]] = {}


def _cpu_percent(build_id: int, st: dict) -> float | None:
    cpu = st.get("cpu_stats", {})
    total, system = cpu.get("cpu_usage", {}).get("total_usage"), cpu.get("system_cpu_usage")
    if total is None or system is None:
        return None
    prev = _prev_cpu.get(build_id)
    _prev_cpu[build_id] = (total, system)
    if not prev or system <= prev[1]:
        return None
    return round(100.0 * (total - prev[0]) / (system - prev[1]) * (cpu.get("online_cpus") or 1), 1)


def collect_stats() -> dict:
    out: dict = {"disk": disk_status(), "docker": {}, "previews": []}
    try:
        cl = docker_ops.client()
        info = cl.info()
        out["docker"] = {"cpus": info.get("NCPU"), "mem_total_gb": round(info.get("MemTotal", 0) / 1e9, 1)}
        ctrs = cl.containers.list(filters={"label": "previewer.build_id", "status": "running"})
    except Exception:  # noqa: BLE001 — stats are best effort
        log.exception("docker stats unavailable")
        return out
    ids = {int(c.labels["previewer.build_id"]): c for c in ctrs if c.labels.get("previewer.build_id", "").isdigit()}
    with session_scope() as s:
        names = {b.id: f"{b.branch.project.name} / {b.branch.name}"
                 for b in s.scalars(select(Build).where(Build.id.in_(list(ids))))}
    for build_id, c in sorted(ids.items()):
        try:
            st = c.stats(stream=False, one_shot=True)
        except Exception:  # noqa: BLE001
            continue
        mem = st.get("memory_stats", {})
        usage = (mem.get("usage") or 0) - (mem.get("stats", {}).get("inactive_file") or 0)
        out["previews"].append({"build_id": build_id, "name": names.get(build_id, c.name),
                                "mem_mb": round(usage / 1e6), "cpu_percent": _cpu_percent(build_id, st)})
    for gone in set(_prev_cpu) - set(ids):
        _prev_cpu.pop(gone, None)
    return out


def stats() -> str:
    data = collect_stats()
    data["ts"] = utcnow().isoformat()
    path = _cache_dir() / "stats.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)
    return f"{len(data['previews'])} preview(s), {data['disk']['free_gb']} GB free"


def read_stats() -> dict:
    try:
        return json.loads((_cache_dir() / "stats.json").read_text())
    except (OSError, ValueError):
        return {}


def _minutes(key: str):
    return lambda: timedelta(minutes=max(1, int(float(_setting(key)))))


TASKS: list[Task] = [
    Task("poll", _minutes("poll_minutes"), poll),
    Task("visits", lambda: timedelta(seconds=30), visits),
    Task("disk_guard", lambda: timedelta(seconds=30), disk_guard),
    Task("sleep_idle", lambda: timedelta(minutes=1), sleep_idle),
    Task("stats", lambda: timedelta(minutes=1), stats),
]
