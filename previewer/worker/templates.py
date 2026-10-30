"""Database templates: a ready database per Odoo version / edition / demo / image / Enterprise
commit with base (+ web_enterprise) installed. Builds copy it (CREATE DATABASE ... TEMPLATE)
with its filestore and only install their own modules, instead of installing Odoo from scratch.

A template that cannot be made never fails a build: the build falls back to a full install.
"""
import hashlib
import logging
import os
import shutil
import threading
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from .. import appsettings
from ..config import get_settings
from ..db import session_scope
from ..models import DbTemplate, utcnow
from ..security import random_secret
from . import docker_ops, pg
from .logs import BuildLog
from .scheduler import Task

log = logging.getLogger("previewer.templates")

MIN_FREE_BYTES = 3 * 1024**3       # do not make a template with less free disk than this
MAX_AGE = timedelta(days=14)       # Enterprise copies expire ~30 days after creation
RETRY_FAILED_AFTER = timedelta(hours=1)
UNUSED_FOR = timedelta(days=7)
KEEP_PER_FLAVOUR = 2
WAIT_FOR_OTHER = 30 * 60           # seconds a build waits for a template another build is making

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def make_key(version: str, edition: str, demo: bool, image_id: str, enterprise_ref: str) -> str:
    return f"{version}|{edition}|demo={int(demo)}|{image_id}|ent={enterprise_ref}"


def key_hash(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def template_dir(key: str) -> Path:
    return get_settings().data_dir / "templates" / key_hash(key)


def template_db(key: str) -> str:
    return f"tpl_{key_hash(key)}"


def base_modules(edition: str) -> list[str]:
    return ["base", "web_enterprise"] if edition == "enterprise" else ["base"]


def enterprise_ref(enterprise_dir: str) -> str:
    """checkouts/enterprise-<repo hash>-<branch>-<sha12> -> '<repo hash>-<branch>-<sha12>'."""
    return Path(enterprise_dir).name.removeprefix("enterprise-") if enterprise_dir else ""


def enabled() -> bool:
    with session_scope() as s:
        return appsettings.get(s, "use_db_templates") == "1"


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _free_bytes() -> int:
    return shutil.disk_usage(get_settings().data_dir).free


def ensure(spec, blog: BuildLog) -> DbTemplate | None:
    """A ready template for this build, making it if needed; None = do a full install."""
    try:
        key = make_key(spec.version, spec.edition, spec.demo, docker_ops.image_id(spec.base_image),
                       enterprise_ref(spec.enterprise_dir))
    except Exception as e:  # noqa: BLE001 — no Docker / image: just install normally
        blog.write(f"Database template skipped ({type(e).__name__}: {e}); installing from scratch\n")
        return None
    with _lock(key):
        state = _claim(key, spec)
        if isinstance(state, DbTemplate):
            return state
        if state == "skip":
            return None
        if state == "wait":
            return _wait_for(key, blog)
        return _make(key, spec, blog)


def _claim(key: str, spec):
    """Ready template, 'make' (row now ours, status building), 'wait' or 'skip'."""
    with session_scope() as s:
        t = s.scalars(select(DbTemplate).where(DbTemplate.key == key)).first()
        now = utcnow()
        if t and t.status == "ready" and now - t.created_at < MAX_AGE and pg.database_exists(t.db_name):
            t.last_used_at = now
            s.flush()
            s.expunge(t)
            return t
        if t and t.status == "building" and now - t.created_at < timedelta(seconds=WAIT_FOR_OTHER):
            return "wait"
        if t and t.status == "failed" and now - t.created_at < RETRY_FAILED_AFTER:
            return "skip"
        if _free_bytes() < MIN_FREE_BYTES:
            return "skip"
        if t:  # stale, failed long ago, or its database vanished: rebuild under the same key
            _drop_files(t.db_name, key)
            s.delete(t)
            s.flush()
        s.add(DbTemplate(key=key, version=spec.version, edition=spec.edition, demo=spec.demo,
                         image=spec.base_image, enterprise_ref=enterprise_ref(spec.enterprise_dir),
                         db_name=template_db(key), modules=",".join(base_modules(spec.edition)),
                         status="building"))
        return "make"


def _wait_for(key: str, blog: BuildLog) -> DbTemplate | None:
    blog.step("Waiting for the database template another build is making")
    deadline = time.monotonic() + WAIT_FOR_OTHER
    while time.monotonic() < deadline:
        time.sleep(5)
        with session_scope() as s:
            t = s.scalars(select(DbTemplate).where(DbTemplate.key == key)).first()
            if t is None or t.status == "failed":
                return None
            if t.status == "ready":
                s.expunge(t)
                return t
    return None


def _make(key: str, spec, blog: BuildLog) -> DbTemplate | None:
    db, tdir = template_db(key), template_dir(key)
    mods = base_modules(spec.edition)
    label = f"{spec.version} {spec.edition.capitalize()}{' with demo' if spec.demo else ''}"
    blog.step(f"Making database template {label} ({', '.join(mods)}); later builds reuse it")
    started = time.monotonic()
    try:
        pg.drop_database(db)
        shutil.rmtree(tdir, ignore_errors=True)
        (tdir / "data").mkdir(parents=True)
        _chown(tdir / "data")
        (tdir / "odoo.conf").write_text(docker_ops.render_conf(
            db_name=db, admin_passwd=random_secret(),
            addons=docker_ops.addons_path(0, bool(spec.enterprise_dir), with_src=False)))
        mounts = docker_ops.template_mounts(tdir / "odoo.conf", tdir / "data",
                                            Path(spec.enterprise_dir) if spec.enterprise_dir else None)
        outcome: dict = {}
        rc = docker_ops.run_init(image=spec.base_image, build_id=spec.build_id,
                                 args=["-d", db, "-i", ",".join(mods), "--stop-after-init", *spec.demo_flags],
                                 mounts_=mounts, mem_limit=spec.build_mem_limit, timeout_s=spec.timeout_s,
                                 log=blog, outcome=outcome, name=f"tpl-{key_hash(key)}-init")
        states = pg.module_states(db, mods) if rc == 0 else {}
        bad = [m for m in mods if states.get(m) != "installed"]
        if rc != 0 or bad:
            raise RuntimeError(f"exit code {rc}" if rc != 0 else f"not installed: {', '.join(bad)}")
    except Exception as e:  # noqa: BLE001 — a template failure only costs the speed-up
        _mark(key, "failed", f"{type(e).__name__}: {e}")
        _drop_files(db, key)
        blog.step(f"Database template {label} could not be made ({e}); installing from scratch")
        return None
    _mark(key, "ready")
    blog.step(f"Database template {label} ready in {time.monotonic() - started:.0f}s "
              f"({_size_mb(db, tdir)} MB)")
    with session_scope() as s:
        t = s.scalars(select(DbTemplate).where(DbTemplate.key == key)).first()
        t.last_used_at = utcnow()
        s.flush()
        s.expunge(t)
        return t


def _mark(key: str, status: str, error: str | None = None) -> None:
    with session_scope() as s:
        t = s.scalars(select(DbTemplate).where(DbTemplate.key == key)).first()
        if t:
            t.status, t.error = status, error


def copy_into(t: DbTemplate, db_name: str, filestore: Path) -> None:
    """New build database from the template, with its own identity and a copy of its filestore
    (Odoo keeps compiled assets there)."""
    pg.create_from_template(db_name, t.db_name, get_settings().odoo_db_user)
    pg.reset_database_identity(db_name)
    src = template_dir(t.key) / "data" / "filestore" / t.db_name
    if src.exists():
        dest = filestore / "filestore" / db_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, dirs_exist_ok=True)
        _chown(filestore / "filestore")


def _chown(path: Path) -> None:
    uid = docker_ops.ODOO_UID
    try:
        os.chown(path, uid, uid)
        for root, dirs, files in os.walk(path):
            for n in dirs + files:
                os.chown(os.path.join(root, n), uid, uid)
    except OSError:
        pass  # Docker Desktop file sharing does not enforce ownership


def _size_mb(db: str, tdir: Path) -> int:
    files = sum(f.stat().st_size for f in tdir.rglob("*") if f.is_file())
    return round((pg.database_size(db) + files) / 1024**2)


def _drop_files(db: str, key: str) -> None:
    try:
        pg.drop_database(db)
    except Exception:  # noqa: BLE001
        log.exception("could not drop template database %s", db)
    shutil.rmtree(template_dir(key), ignore_errors=True)


def prune() -> str:
    """Drop templates unused for 7 days, failed ones, any beyond the 2 most recently used per
    version/edition/demo, and template folders no row knows about."""
    now = utcnow()
    with session_scope() as s:
        rows = s.scalars(select(DbTemplate).order_by(DbTemplate.last_used_at.desc().nullslast(),
                                                     DbTemplate.created_at.desc())).all()
        kept: dict[tuple, int] = {}
        drop = []
        for t in rows:
            flavour = (t.version, t.edition, t.demo)
            used = t.last_used_at or t.created_at
            if t.status == "building" and now - t.created_at < timedelta(seconds=WAIT_FOR_OTHER):
                continue
            if t.status != "ready" or now - used > UNUSED_FOR or now - t.created_at > MAX_AGE \
                    or kept.get(flavour, 0) >= KEEP_PER_FLAVOUR:
                drop.append((t.id, t.key, t.db_name))
                continue
            kept[flavour] = kept.get(flavour, 0) + 1
        known = {key_hash(t.key) for t in rows} - {key_hash(k) for _, k, _ in drop}
    for tid, key, db in drop:
        with _lock(key):
            _drop_files(db, key)
            with session_scope() as s:
                t = s.get(DbTemplate, tid)
                if t:
                    s.delete(t)
    root = get_settings().data_dir / "templates"
    orphans = [d for d in root.iterdir() if d.is_dir() and d.name not in known] if root.exists() else []
    for d in orphans:
        shutil.rmtree(d, ignore_errors=True)
    return f"dropped {len(drop)} template(s), {len(orphans)} orphan folder(s); kept {sum(kept.values())}"


TASKS = [Task("prune_db_templates", lambda: timedelta(hours=6), prune)]
