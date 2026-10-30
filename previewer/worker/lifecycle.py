"""stop / start / wake / delete / pull jobs."""
from .. import appsettings
from ..db import session_scope
from ..models import Build, OdooVersion, utcnow
from ..naming import container_name
from . import docker_ops
from .logs import BuildLog
from .pipeline import Followers, teardown


class JobError(Exception):
    pass


def stop(build_id: int) -> None:
    docker_ops.stop_container(container_name(build_id))
    with session_scope() as s:
        b = s.get(Build, build_id)
        if b and b.status in ("running", "sleeping"):
            b.status = "stopped"
    BuildLog(build_id).step("Preview stopped")


def sleep(build_id: int, reason: str) -> None:
    """Stop an idle preview but keep its database and filestore; a visit wakes it."""
    with session_scope() as s:
        b = s.get(Build, build_id)
        if not b or b.status != "running":
            return
    docker_ops.stop_container(container_name(build_id))
    with session_scope() as s:
        b = s.get(Build, build_id)
        if b and b.status == "running":
            b.status = "sleeping"
    BuildLog(build_id).step(f"Preview asleep: {reason}")


def start(build_id: int, followers: Followers) -> None:
    """Start (or wake) a stopped/sleeping preview and wait until it is healthy."""
    name = container_name(build_id)
    ctr = docker_ops.get_container(name)
    if ctr is None:
        raise JobError(f"Container {name} no longer exists; rebuild the branch")
    with session_scope() as s:
        timeout = appsettings.get_int(s, "health_timeout_seconds")
    if ctr.status != "running":
        ctr.start()
    followers.start(build_id)
    problem = docker_ops.wait_healthy(name, timeout)
    if problem:
        raise JobError(f"Preview did not become healthy: {problem}")
    with session_scope() as s:
        b = s.get(Build, build_id)
        if b:
            woke = b.status == "sleeping"
            b.status = "running"
            b.last_accessed_at = utcnow()  # or the idle check would put it straight back to sleep
    BuildLog(build_id).step("Preview woke up" if b and woke else "Preview started")
    from .automation import enforce_running_limit
    enforce_running_limit(exclude=build_id)


def delete(payload: dict) -> None:
    """Payload snapshot {build_id, container_id, db_name, purge_log?}: the row may already be
    gone. purge_log also removes the build log (a user deleting the build)."""
    build_id = payload["build_id"]
    if payload.get("container_id"):
        docker_ops.remove_container(payload["container_id"])
    teardown(build_id, payload.get("db_name"), keep_log=not payload.get("purge_log"))
    with session_scope() as s:
        b = s.get(Build, build_id)
        if b:
            b.status, b.container_id = "removed", None


def pull(version: str) -> None:
    with session_scope() as s:
        v = s.get(OdooVersion, version)
        if v is None:
            raise JobError(f"Unknown Odoo version {version}")
        image = v.docker_image
    docker_ops.pull(image)
