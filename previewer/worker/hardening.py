"""Brings previews made by older versions up to the current isolation, at every worker start
(idempotent, cheap when there is nothing to do):

- the app database accepts connections from the app superuser only;
- database templates owned by the old shared preview role are dropped (remade on next use);
- each live build's database, and every table in it, moves from the shared role to the build's
  own role; its odoo.conf gets the new login;
- preview containers still on the control network are recreated on the preview network;
- the shared role loses CREATEDB, and its login once no database depends on it.
"""
import logging
import re

from sqlalchemy import select

from .. import appsettings
from ..config import get_settings
from ..db import session_scope
from ..models import Build, DbTemplate
from ..naming import container_name
from ..security import random_secret
from . import docker_ops, pg, templates
from .pipeline import build_dir

log = logging.getLogger("previewer.hardening")

LIVE = ("running", "sleeping", "stopped")


def set_conf_login(text: str, user: str, password: str, maxconn: int) -> str:
    """odoo.conf with db_user / db_password / db_maxconn set (added when missing)."""
    values = {"db_user": user, "db_password": password, "db_maxconn": str(maxconn)}
    lines = text.splitlines()
    for key, value in values.items():
        pattern = re.compile(rf"^\s*{key}\s*=")
        hits = [i for i, line in enumerate(lines) if pattern.match(line)]
        if hits:
            lines[hits[0]] = f"{key} = {value}"
            for i in reversed(hits[1:]):
                del lines[i]
        else:
            lines.append(f"{key} = {value}")
    return "\n".join(lines) + "\n"


def _drop_old_templates() -> int:
    c = get_settings()
    with session_scope() as s:
        rows = [(t.id, t.key, t.db_name) for t in s.scalars(select(DbTemplate))]
    dropped = 0
    for tid, key, db in rows:
        owner = pg.database_owner(db)
        if owner is None or owner == c.postgres_user:
            continue
        templates._drop_files(db, key)
        with session_scope() as s:
            t = s.get(DbTemplate, tid)
            if t:
                s.delete(t)
        dropped += 1
    return dropped


def _on_control_network(ctr) -> bool:
    nets = (ctr.attrs.get("NetworkSettings") or {}).get("Networks") or {}
    return get_settings().docker_network in nets or get_settings().preview_network not in nets


def _recreate(build_id: int, branch_id: int, host: str, db_name: str, ctr) -> None:
    """Same image, mounts and limits, on the preview network with current labels; started
    again only if it was running."""
    with session_scope() as s:
        timeout = appsettings.get_int(s, "health_timeout_seconds")
    hc = ctr.attrs.get("HostConfig") or {}
    was_running = ctr.status == "running"
    new = docker_ops.run_preview(
        image=ctr.attrs["Config"]["Image"], build_id=build_id, branch_id=branch_id, host=host,
        db_name=db_name, mounts_=docker_ops.mounts_of(ctr), mem_limit=hc.get("Memory") or "1536m",
        cpus=(hc.get("NanoCpus") or 1_000_000_000) / 1_000_000_000, health_timeout_s=timeout,
        start=was_running)
    with session_scope() as s:
        b = s.get(Build, build_id)
        if b:
            b.container_id = new.id


def _secure_build(build_id: int, branch_id: int, host: str, db_name: str, owner: str | None) -> list[str]:
    c = get_settings()
    role = pg.build_role(build_id)
    done = []
    if owner and owner != role:
        password = random_secret()
        pg.ensure_role(role, password)
        pg.hand_over(db_name, owner, role)
        conf = build_dir(build_id) / "odoo.conf"
        if conf.exists():
            conf.write_text(set_conf_login(conf.read_text(), role, password, c.odoo_db_maxconn))
        done.append("own database role")
    ctr = docker_ops.get_container(container_name(build_id))
    if ctr is not None and (done or _on_control_network(ctr)):
        _recreate(build_id, branch_id, host, db_name, ctr)
        done.append("preview network")
    return done


def secure_legacy() -> str:
    c = get_settings()
    pg.protect_app_database()
    docker_ops.check_preview_network()
    dropped = _drop_old_templates()

    with session_scope() as s:
        builds = [(b.id, b.branch_id, b.branch.host if b.branch else "", b.db_name)
                  for b in s.scalars(select(Build).where(Build.status.in_(LIVE), Build.db_name.is_not(None)))]
    owners = {bid: pg.database_owner(db) for bid, _, _, db in builds}
    legacy = {bid: owner for bid, owner in owners.items()
              if owner and owner != pg.build_role(bid) and owner != c.postgres_user}
    if any(owner == c.postgres_user for owner in owners.values()):
        log.warning("some preview databases belong to the app superuser; left as they are")

    # REASSIGN OWNED (pg.hand_over) also moves databases the old role owns. So first every
    # database it owns gets its final owner: live builds their own role, leftovers the superuser.
    keep = {db: bid for bid, _, _, db in builds if bid in legacy}
    for old in set(legacy.values()):
        for db in pg.databases_owned_by(old):
            if db in keep:
                pg.ensure_role(pg.build_role(keep[db]), random_secret())
                pg.set_database_owner(db, pg.build_role(keep[db]))
            else:
                pg.set_database_owner(db, c.postgres_user)

    secured, failed = 0, 0
    for bid, branch_id, host, db in builds:
        try:
            if _secure_build(bid, branch_id, host, db, legacy.get(bid)):
                secured += 1
        except Exception:  # noqa: BLE001 — one preview never blocks the worker
            failed += 1
            log.exception("could not secure build %s", bid)
    pg.lock_shared_role()
    return (f"secured {secured} preview(s), {failed} failed, dropped {dropped} old template(s)"
            if secured or failed or dropped else "nothing to secure")
