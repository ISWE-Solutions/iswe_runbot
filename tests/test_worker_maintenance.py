import gzip
import os
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from previewer.config import get_settings
from previewer.models import Branch, Build, Job, Project, utcnow
from previewer.worker import maintenance


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    torn = []
    monkeypatch.setattr(maintenance, "teardown", lambda bid, db, keep_log=True: torn.append((bid, db)))
    monkeypatch.setattr(maintenance.pg, "database_exists", lambda name: False)
    monkeypatch.setattr(maintenance.docker_ops, "preview_containers", lambda: [])
    return SimpleNamespace(root=tmp_path, torn=torn)


def _branch(s, name="v18", kind="repo", **kw):
    p = s.query(Project).filter_by(slug=f"p-{kind}").first()
    if p is None:
        p = Project(name=kind, slug=f"p-{kind}", repo_url="https://github.com/a/x" if kind == "repo" else "",
                    default_odoo_version="18.0", kind=kind)
        s.add(p)
    br = Branch(project=p, name=name, slug=name, host=f"{name}.{kind}.localhost", last_commit_sha="a" * 40, **kw)
    s.add(br)
    s.flush()
    return br


def _build(s, br, status, days_ago=0, **kw):
    b = Build(branch_id=br.id, commit_sha="a" * 40, odoo_version="18.0", status=status,
              created_at=utcnow() - timedelta(days=days_ago), **kw)
    s.add(b)
    s.flush()
    return b


def test_cleanup_deleted_after_grace(s, data):
    old = _branch(s, "gone", deleted_at=utcnow() - timedelta(hours=30))
    b1 = _build(s, old, "running", container_id="c1", db_name="db1")
    _build(s, old, "failed", db_name="db0")
    recent = _branch(s, "just-gone", deleted_at=utcnow() - timedelta(hours=2))
    s.commit()
    old_id, recent_id, b1_id = old.id, recent.id, b1.id
    msg = maintenance.cleanup_deleted()
    s.expire_all()
    assert "removed 1" in msg and s.get(Branch, old_id) is None and s.get(Branch, recent_id) is not None
    payloads = [j.payload for j in s.query(Job).filter_by(type="delete")]
    assert {"build_id": b1_id, "container_id": "c1", "db_name": "db1", "purge_log": True} in payloads
    assert len(payloads) == 2


def test_cleanup_stale_stop_clear_and_delete(s, data):
    now = utcnow()
    stale = _branch(s, "old", last_commit_at=now - timedelta(days=40))
    live = _build(s, stale, "running", finished_at=now - timedelta(days=40))
    fresh = _branch(s, "fresh", last_commit_at=now - timedelta(days=2))
    _build(s, fresh, "running")
    ancient = _branch(s, "ancient", last_commit_at=now - timedelta(days=70), stale_since=now - timedelta(days=40))
    _build(s, ancient, "stopped", container_id="c9", db_name="db9", finished_at=now - timedelta(days=70))
    visited = _branch(s, "visited", last_commit_at=now - timedelta(days=70), stale_since=now - timedelta(days=40))
    _build(s, visited, "stopped", last_accessed_at=now - timedelta(days=1))
    pushed = _branch(s, "pushed", last_commit_at=now - timedelta(hours=1), stale_since=now - timedelta(days=5))
    _build(s, pushed, "running")
    s.commit()
    msg = maintenance.cleanup_stale()
    s.expire_all()
    assert msg.startswith("stopped 1 stale, cleared 2, deleted 1")
    assert "(stopped: p-repo/old)" in msg
    assert s.get(Branch, stale.id).stale_since is not None
    stop = s.query(Job).filter_by(type="stop").one()
    assert stop.payload == {"build_id": live.id}
    assert s.get(Branch, fresh.id).stale_since is None
    a = s.get(Branch, ancient.id)
    assert not a.enabled and a.builds == [] and a.stale_since is None
    assert s.query(Job).filter_by(type="delete").one().payload["db_name"] == "db9"
    assert s.get(Branch, visited.id).stale_since is None and s.get(Branch, pushed.id).stale_since is None


def test_cleanup_stale_exemptions(s, data):
    now = utcnow()
    starred = _branch(s, "v19", last_commit_at=now - timedelta(days=90), sticky=True)
    _build(s, starred, "running", finished_at=now - timedelta(days=90))
    visited = _branch(s, "busy", last_commit_at=now - timedelta(days=90))
    _build(s, visited, "running", finished_at=now - timedelta(days=90), last_accessed_at=now - timedelta(days=3))
    rebuilt = _branch(s, "rebuilt", last_commit_at=now - timedelta(days=90))
    _build(s, rebuilt, "running", finished_at=now - timedelta(hours=1))  # never visited, built recently
    marked = _branch(s, "v18", last_commit_at=now - timedelta(days=90), sticky=True,
                     stale_since=now - timedelta(days=5))
    _build(s, marked, "stopped", finished_at=now - timedelta(days=90))
    keep_visited = _branch(s, "keep", last_commit_at=now - timedelta(days=90), stale_since=now - timedelta(days=40))
    _build(s, keep_visited, "stopped", finished_at=now - timedelta(days=90), last_accessed_at=now - timedelta(days=41))
    s.commit()
    msg = maintenance.cleanup_stale()
    s.expire_all()
    assert s.query(Job).filter_by(type="stop").count() == 0 and s.query(Job).filter_by(type="delete").count() == 0
    assert all(s.get(Branch, b.id).stale_since is None for b in (starred, visited, rebuilt, marked))
    assert s.get(Branch, keep_visited.id).enabled  # visited 41 days ago: inside the 60-day window
    assert msg.startswith("stopped 0 stale, cleared 1, deleted 0")


def test_cleanup_builds_keeps_latest_failure(s, data):
    br = _branch(s)
    f_old = _build(s, br, "failed", days_ago=30)
    f_new = _build(s, br, "failed", days_ago=20)
    r_old = _build(s, br, "removed", days_ago=30)
    r_left = _build(s, br, "removed", db_name="dbx")
    live = _build(s, br, "running")
    s.commit()
    logs = data.root / "logs"
    logs.mkdir()
    for b in (f_old, f_new):
        (logs / f"{b.id}.log").write_text("x")
    (data.root / "builds" / str(r_left.id)).mkdir(parents=True)
    ids = {k: b.id for k, b in dict(f_old=f_old, f_new=f_new, r_old=r_old, r_left=r_left, live=live).items()}
    msg = maintenance.cleanup_builds()
    s.expire_all()
    assert s.get(Build, ids["f_old"]) is None and not (logs / f"{ids['f_old']}.log").exists()
    assert s.get(Build, ids["f_new"]) is not None and (logs / f"{ids['f_new']}.log").exists()
    assert s.get(Build, ids["r_old"]) is None and s.get(Build, ids["live"]) is not None
    assert (ids["r_left"], "dbx") in data.torn and "dropped 2" in msg


def test_prune_enterprise(s, data, monkeypatch):
    co = data.root / "checkouts"
    names = ["enterprise-aaaa1111-18.0-000000000001", "enterprise-aaaa1111-18.0-000000000002",
             "enterprise-aaaa1111-18.0-000000000003", "enterprise-bbbb2222-19.0-000000000009"]
    old = time.time() - 3 * 86400
    for i, n in enumerate(names):
        (co / n).mkdir(parents=True)
        (co / n / ".complete").touch()
        (co / n / "f.txt").write_text("x" * 100)
        os.utime(co / n, (old + i, old + i))
    mounted = SimpleNamespace(attrs={"Mounts": [{"Source": f"/host_mnt{co}/{names[0]}"}]},
                              labels={"previewer.build_id": "1"}, name="pv-1")
    orphan = SimpleNamespace(attrs={"Mounts": []}, labels={"previewer.build_id": "77"}, name="pv-77",
                             remove=lambda force: data.torn.append(("ctr", 77)))
    monkeypatch.setattr(maintenance.docker_ops, "preview_containers", lambda: [mounted, orphan])
    br = _branch(s)
    known = _build(s, br, "running")
    s.commit()
    (data.root / "builds" / str(known.id)).mkdir(parents=True)
    (data.root / "builds" / "999999").mkdir(parents=True)
    dry = maintenance.prune_enterprise(dry_run=True)
    assert "would delete 3" in dry and (co / names[1]).exists()
    maintenance.prune_enterprise()
    assert (co / names[0]).exists()       # mounted by a container
    assert not (co / names[1]).exists()   # old, unmounted
    assert (co / names[2]).exists()       # newest for 18.0
    assert (co / names[3]).exists()       # newest for 19.0
    assert (data.root / "builds" / str(known.id)).exists() and not (data.root / "builds" / "999999").exists()
    assert ("ctr", 77) in data.torn


def test_prune_images_only_ours(data, monkeypatch):
    removed, prune_filters = [], []

    def img(id_, tags, labels=None, size=10_000_000):
        return SimpleNamespace(id=id_, tags=tags, labels=labels or {}, attrs={"Size": size}, short_id=id_[:8])

    images = [img("sha:ours-unused", ["previewer-odoo:19.0-abc"]), img("sha:ours-used", ["previewer-odoo:18.0-def"]),
              img("sha:odoo", ["odoo:19"]), img("sha:other", ["locas-helpdesk-api:latest"]),
              img("sha:labelled", [], {"previewer.image": "1"})]
    client = SimpleNamespace(
        containers=SimpleNamespace(list=lambda all: [SimpleNamespace(attrs={"Image": "sha:ours-used"})]),
        images=SimpleNamespace(list=lambda: images,
                               remove=lambda i, force: removed.append(i),
                               prune=lambda filters: prune_filters.append(filters) or {"SpaceReclaimed": 5_000_000}))
    monkeypatch.setattr(maintenance.docker_ops, "client", lambda: client)
    msg = maintenance.prune_images()
    assert removed == ["sha:ours-unused", "sha:labelled"]
    assert prune_filters == [{"dangling": True, "label": "previewer.image=1"}]
    assert "removed 2 image(s), reclaimed 25 MB" == msg


def test_backup_writes_gzip_and_keeps_n_days(s, data, monkeypatch):
    calls = {}

    class Api:
        def exec_create(self, cid, cmd):
            calls["cmd"] = cmd
            return {"Id": "e1"}

        def exec_start(self, eid, stream, demux):
            yield b"-- dump\n", None
            yield b"CREATE TABLE x();\n", b"warning\n"

        def exec_inspect(self, eid):
            return {"ExitCode": 0}

    client = SimpleNamespace(containers=SimpleNamespace(list=lambda filters: [SimpleNamespace(id="pg1")]), api=Api())
    monkeypatch.setattr(maintenance.docker_ops, "client", lambda: client)
    out = data.root / "backups"
    out.mkdir()
    stale = out / "previewer-2026-01-01.sql.gz"
    stale.write_bytes(b"old")
    os.utime(stale, (time.time() - 40 * 86400,) * 2)
    msg = maintenance.backup()
    files = sorted(p.name for p in out.iterdir())
    assert files == [f"previewer-{utcnow():%Y-%m-%d}.sql.gz"] and "removed 1 old backup" in msg
    assert gzip.decompress((out / files[0]).read_bytes()) == b"-- dump\nCREATE TABLE x();\n"
    assert calls["cmd"][:2] == ["pg_dump", "-U"]


def test_backup_failure_raises_and_leaves_nothing(data, monkeypatch):
    class Api:
        def exec_create(self, cid, cmd):
            return {"Id": "e1"}

        def exec_start(self, eid, stream, demux):
            yield None, b"pg_dump: error: connection failed\n"

        def exec_inspect(self, eid):
            return {"ExitCode": 1}

    client = SimpleNamespace(containers=SimpleNamespace(list=lambda filters: [SimpleNamespace(id="pg1")]), api=Api())
    monkeypatch.setattr(maintenance.docker_ops, "client", lambda: client)
    with pytest.raises(RuntimeError, match="connection failed"):
        maintenance.backup()
    assert list((data.root / "backups").iterdir()) == []


def test_tasks_registered():
    assert [t.name for t in maintenance.TASKS] == [
        "cleanup_deleted", "cleanup_stale", "cleanup_builds", "prune_enterprise", "prune_images", "backup"]
