"""Periodic worker tasks (polling, sleep, cleanup, backups...). Each task module exposes
TASKS = [Task(...)]; last runs are stored in `scheduler_state` so a restart does not re-run
a nightly backup. Tasks run one at a time on their own thread, never inside a build slot."""
import logging
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from ..db import session_scope
from ..models import SchedulerState, utcnow

log = logging.getLogger("previewer.scheduler")
TICK_SECONDS = 15


@dataclass
class Task:
    name: str
    every: Callable[[], timedelta]   # read at each tick so System-page settings apply live
    run: Callable[[], str | None]    # returns a short message for the System page


def all_tasks() -> list[Task]:
    from . import automation, maintenance, templates
    return [*automation.TASKS, *maintenance.TASKS, *templates.TASKS]


def due(task: Task) -> bool:
    with session_scope() as s:
        st = s.get(SchedulerState, task.name)
        return st is None or st.last_run_at is None or utcnow() - st.last_run_at >= task.every()


def record(name: str, ok: bool, message: str) -> None:
    with session_scope() as s:
        st = s.get(SchedulerState, name) or SchedulerState(name=name)
        st.last_run_at, st.last_ok, st.last_message = utcnow(), ok, message[:2000]
        s.add(st)


def run_forever(stop: threading.Event) -> None:
    while not stop.is_set():
        for task in all_tasks():
            if stop.is_set():
                break
            try:
                if not due(task):
                    continue
                message = task.run() or ""
                record(task.name, True, message)
            except Exception as e:  # noqa: BLE001 — a failing task must not stop the others
                log.exception("scheduled task %s failed", task.name)
                record(task.name, False, f"{type(e).__name__}: {e}")
        stop.wait(TICK_SECONDS)
