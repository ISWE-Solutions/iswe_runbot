"""Worker process: claims jobs from the Postgres queue and runs them.

N build threads (setting `max_concurrent_builds`) take `build` jobs; one more thread takes
everything else so a stop or sync never waits behind a 20-minute build.
"""
import json
import logging
import shutil
import signal
import threading
import time

from sqlalchemy import select, update

from .. import appsettings, jobs
from ..config import get_settings
from ..db import SessionLocal, session_scope
from ..models import Build, Job, SchedulerState, utcnow
from . import automation, docker_ops, lifecycle, pg, pipeline, scheduler
from .sync import sync_project

log = logging.getLogger("previewer.worker")

OTHER_TYPES = ("sync", "stop", "start", "wake", "delete", "pull")
POLL_SECONDS = 2
HEARTBEAT_SECONDS = 10


class Worker:
    def __init__(self):
        self.stop_event = threading.Event()
        self.followers = pipeline.Followers()
        self.running: dict[int, str] = {}
        self._lock = threading.Lock()

    # --- startup -------------------------------------------------------------
    def recover(self) -> None:
        """Jobs/builds left mid-flight by a previous worker cannot be resumed."""
        with session_scope() as s:
            s.execute(update(Job).where(Job.status == "running").values(
                status="failed", error="worker restarted", finished_at=utcnow()))
            stale = s.scalars(select(Build).where(Build.status == "building")).all()
            for b in stale:
                b.status, b.error_summary, b.finished_at = "failed", "Worker restarted during the build", utcnow()
            stale_ids = [(b.id, b.db_name) for b in stale]
            live = s.scalars(select(Build.id).where(Build.status == "running")).all()
        for build_id, db_name in stale_ids:
            pipeline.teardown(build_id, db_name, keep_log=True)
        for build_id in live:
            self.followers.start(build_id)

    # --- job execution ---------------------------------------------------------
    def handle(self, job: Job) -> None:
        p = job.payload or {}
        if job.type == "build":
            pipeline.run_build(p["build_id"], self.followers)
        elif job.type == "sync":
            with session_scope() as s:
                counts = sync_project(s, p["project_id"])
            log.info("sync project %s: %s", p["project_id"], counts)
        elif job.type == "stop":
            lifecycle.stop(p["build_id"])
        elif job.type in ("start", "wake"):
            lifecycle.start(p["build_id"], self.followers)
        elif job.type == "delete":
            lifecycle.delete(p)
        elif job.type == "pull":
            lifecycle.pull(p["version"])
        else:
            raise lifecycle.JobError(f"Unknown job type {job.type}")

    def _disk_low(self) -> bool:
        """Checked right before a build is claimed (the scheduler's disk_guard runs only
        every 30 s): with too little free space, leave build jobs queued."""
        try:
            with session_scope() as s:
                need_gb = float(appsettings.get(s, "disk_min_free_gb"))
            return shutil.disk_usage(get_settings().data_dir).free / 1e9 < need_gb
        except Exception:  # noqa: BLE001 — never block the queue on a failed check
            return False

    def loop(self, types: tuple[str, ...], name: str) -> None:
        while not self.stop_event.is_set():
            if "build" in types and self._disk_low():
                self.stop_event.wait(30)
                continue
            s = SessionLocal()
            try:
                job = jobs.claim(s, types)
                if job is None:
                    self.stop_event.wait(POLL_SECONDS)
                    continue
                with self._lock:
                    self.running[job.id] = job.type
                log.info("[%s] job %s %s %s", name, job.id, job.type, job.payload)
                error = None
                try:
                    self.handle(job)
                except Exception as e:  # noqa: BLE001 — record any failure on the job
                    log.exception("job %s failed", job.id)
                    error = f"{type(e).__name__}: {e}"
                jobs.finish(s, job, error)
                with self._lock:
                    self.running.pop(job.id, None)
            except Exception:
                log.exception("[%s] queue error", name)
                self.stop_event.wait(POLL_SECONDS)
            finally:
                s.close()

    def heartbeat(self) -> None:
        path = get_settings().data_dir / "worker.heartbeat"
        while not self.stop_event.is_set():
            with self._lock:
                running = sorted(self.running)
            data = {"ts": time.time(), "docker_ok": docker_ops.docker_ok(), "running_jobs": running}
            try:
                data.update(self._heartbeat_stats())
            except Exception:  # noqa: BLE001 — stats never stop the heartbeat
                log.exception("heartbeat stats failed")
            try:
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data))
                tmp.replace(path)
            except OSError:
                log.exception("heartbeat write failed")
            self.stop_event.wait(HEARTBEAT_SECONDS)

    @staticmethod
    def _heartbeat_stats() -> dict:
        """Disk now; Docker and per-preview usage from the `stats` task (refreshed each
        minute); last run of each scheduled task. Read by the System page."""
        cached = automation.read_stats()
        with session_scope() as s:
            sched = [{"name": st.name, "last_run_at": st.last_run_at.isoformat() if st.last_run_at else None,
                      "ok": st.last_ok, "message": st.last_message}
                     for st in s.scalars(select(SchedulerState).order_by(SchedulerState.name))]
        return {"disk": automation.disk_status(), "docker": cached.get("docker", {}),
                "previews": cached.get("previews", []), "stats_at": cached.get("ts"),
                "scheduler": sched}

    def run(self) -> None:
        c = get_settings()
        for d in (c.repos_dir, c.builds_dir, c.filestore_dir, c.logs_dir, c.checkouts_dir):
            d.mkdir(parents=True, exist_ok=True)
        pg.ensure_odoo_role()
        self.recover()
        with session_scope() as s:
            n_builds = max(1, appsettings.get_int(s, "max_concurrent_builds"))
        threads = [threading.Thread(target=self.heartbeat, name="heartbeat", daemon=True)]
        threads += [threading.Thread(target=self.loop, args=(("build",), f"build-{i}"), daemon=True)
                    for i in range(n_builds)]
        threads.append(threading.Thread(target=self.loop, args=(OTHER_TYPES, "ops"), daemon=True))
        threads.append(threading.Thread(target=scheduler.run_forever, args=(self.stop_event,),
                                        name="scheduler", daemon=True))
        for t in threads:
            t.start()
        log.info("worker started: %d build slot(s)", n_builds)
        while not self.stop_event.is_set():
            self.stop_event.wait(1)
        log.info("worker stopping")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    worker = Worker()
    signal.signal(signal.SIGTERM, lambda *_: worker.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: worker.stop_event.set())
    worker.run()
