"""Database templates: builds copy a ready base/Enterprise database instead of installing
Odoo from scratch (Docker and Postgres are mocked)."""
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from previewer.models import DbTemplate, utcnow
from previewer.worker import logs, pipeline, templates

from test_worker_pipeline import _spec  # noqa: E402 (tests/ is on sys.path)

ENT_DIR = "/data/checkouts/enterprise-ab12cd34-19.0-0123456789ab"


@pytest.fixture(autouse=True)
def clean_templates(s):
    """db_templates is not in conftest's TRUNCATE list."""
    s.query(DbTemplate).delete()
    s.commit()
    yield
    s.rollback()
    s.query(DbTemplate).delete()
    s.commit()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fake Docker + Postgres; records what was called."""
    monkeypatch.setattr(logs, "log_path", lambda build_id: tmp_path / "logs" / f"{build_id}.log")
    monkeypatch.setattr(templates, "get_settings",
                        lambda: SimpleNamespace(data_dir=tmp_path, odoo_db_user="odoo"))
    calls = SimpleNamespace(init=[], created=[], dropped=[], dbs=set(), rc=0, free=50 * 1024**3, delay=0)
    monkeypatch.setattr(templates.docker_ops, "image_id", lambda image: "sha256:abcdef123456")

    def run_init(**kw):
        time.sleep(calls.delay)
        calls.init.append(kw)
        db = kw["args"][1]
        calls.dbs.add(db)
        fs = tmp_path / "templates" / db.removeprefix("tpl_") / "data" / "filestore" / db
        fs.mkdir(parents=True, exist_ok=True)
        (fs / "asset.bin").write_text("x")
        return calls.rc

    monkeypatch.setattr(templates.docker_ops, "run_init", run_init)
    monkeypatch.setattr(templates.pg, "module_states", lambda db, mods: {m: "installed" for m in mods})
    monkeypatch.setattr(templates.pg, "database_exists", lambda db: db in calls.dbs)
    monkeypatch.setattr(templates.pg, "drop_database", lambda db: (calls.dropped.append(db), calls.dbs.discard(db)))
    monkeypatch.setattr(templates.pg, "database_size", lambda db: 50 * 1024**2)
    monkeypatch.setattr(templates.pg, "create_from_template", lambda n, t, o: calls.created.append((n, t, o)))
    monkeypatch.setattr(templates.pg, "reset_database_identity", lambda n: None)
    monkeypatch.setattr(templates, "_free_bytes", lambda: calls.free)
    return calls


def spec(**kw):
    base = dict(version="19.0", edition="enterprise", demo=False, enterprise_dir=ENT_DIR,
                base_image="odoo:19", demo_flags=[], build_id=7)
    base.update(kw)
    return _spec(**base)


def test_key_and_names():
    key = templates.make_key("19.0", "enterprise", True, "sha256:abc", templates.enterprise_ref(ENT_DIR))
    assert key == "19.0|enterprise|demo=1|sha256:abc|ent=ab12cd34-19.0-0123456789ab"
    assert templates.template_db(key) == f"tpl_{templates.key_hash(key)}" and len(templates.template_db(key)) == 16
    assert templates.base_modules("community") == ["base"]
    assert templates.base_modules("enterprise") == ["base", "web_enterprise"]


def test_creates_once_then_reuses(s, env):
    log = logs.BuildLog(7)
    t1 = templates.ensure(spec(), log)
    assert t1 is not None and t1.status == "ready" and t1.modules == "base,web_enterprise"
    args = env.init[0]["args"]
    assert args[:4] == ["-d", t1.db_name, "-i", "base,web_enterprise"] and "--stop-after-init" in args
    assert env.init[0]["name"].startswith("tpl-")
    t2 = templates.ensure(spec(build_id=8), logs.BuildLog(8))
    assert t2.id == t1.id and len(env.init) == 1
    assert "Database template 19.0 Enterprise" in logs.read_log(7)


def test_new_key_for_other_demo_or_enterprise_commit(s, env):
    templates.ensure(spec(), logs.BuildLog(7))
    templates.ensure(spec(demo=True, demo_flags=["--with-demo"]), logs.BuildLog(7))
    templates.ensure(spec(enterprise_dir=ENT_DIR.replace("0123456789ab", "ffffffffffff")), logs.BuildLog(7))
    templates.ensure(spec(edition="community", enterprise_dir=""), logs.BuildLog(7))
    assert len(env.init) == 4 and s.query(DbTemplate).count() == 4
    assert "--with-demo" in env.init[1]["args"]


def test_concurrent_builds_make_it_once(s, env):
    env.delay = 0.3
    got = []
    threads = [threading.Thread(target=lambda i=i: got.append(templates.ensure(spec(build_id=i), logs.BuildLog(i))))
               for i in (1, 2, 3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(env.init) == 1 and len({t.id for t in got}) == 1


def test_failure_falls_back_and_is_not_retried_for_an_hour(s, env):
    env.rc = 255
    assert templates.ensure(spec(), logs.BuildLog(7)) is None
    assert s.query(DbTemplate).one().status == "failed"
    assert "could not be made" in logs.read_log(7)
    env.rc = 0
    assert templates.ensure(spec(), logs.BuildLog(7)) is None and len(env.init) == 1
    row = s.query(DbTemplate).one()
    row.created_at = utcnow() - timedelta(hours=2)
    s.commit()
    assert templates.ensure(spec(), logs.BuildLog(7)) is not None and len(env.init) == 2


def test_low_disk_skips_making_a_template(s, env):
    env.free = 1024**3
    assert templates.ensure(spec(), logs.BuildLog(7)) is None and env.init == []


def test_no_docker_means_full_install(s, env, monkeypatch):
    def boom(image):
        raise RuntimeError("no docker")
    monkeypatch.setattr(templates.docker_ops, "image_id", boom)
    assert templates.ensure(spec(), logs.BuildLog(7)) is None
    assert "installing from scratch" in logs.read_log(7)


def test_build_database_copied_from_template(s, env, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "filestore_dir", lambda build_id: tmp_path / "fs" / str(build_id))
    monkeypatch.setattr(templates, "enabled", lambda: True)
    sp = spec(db_name="pay_v19_abc_b7", modules=["payroll_zm", "bank_zm"])
    assert pipeline._from_template(sp, logs.BuildLog(7))
    t = s.query(DbTemplate).one()
    assert env.created == [("pay_v19_abc_b7", t.db_name, "odoo")]
    assert (tmp_path / "fs" / "7" / "filestore" / "pay_v19_abc_b7" / "asset.bin").exists()
    assert "Copied template 19.0 Enterprise (base, web_enterprise) in" in logs.read_log(7)
    assert "installing payroll_zm, bank_zm" in logs.read_log(7)


def test_disabled_setting_installs_from_scratch(s, env, monkeypatch):
    monkeypatch.setattr(templates, "enabled", lambda: False)
    assert not pipeline._from_template(spec(), logs.BuildLog(7)) and env.init == []


def test_prune_policy(s, env, tmp_path):
    now = utcnow()

    def row(key, used_days, status="ready", demo=False):
        t = DbTemplate(key=key, version="19.0", edition="enterprise", demo=demo, image="odoo:19",
                       db_name=templates.template_db(key), status=status,
                       created_at=now - timedelta(days=used_days), last_used_at=now - timedelta(days=used_days))
        s.add(t)
        (tmp_path / "templates" / templates.key_hash(key)).mkdir(parents=True)
        return key

    keep1, keep2 = row("k1", 0), row("k2", 1)
    extra = row("k3", 2)            # third of the same flavour
    unused = row("k4", 8, demo=True)  # unused for 8 days
    failed = row("k5", 0, status="failed", demo=True)
    s.commit()
    (tmp_path / "templates" / "orphan123456").mkdir()
    msg = templates.prune()
    s.expire_all()
    assert {t.key for t in s.query(DbTemplate)} == {keep1, keep2}
    assert set(env.dropped) >= {templates.template_db(k) for k in (extra, unused, failed)}
    assert not (tmp_path / "templates" / "orphan123456").exists()
    assert (tmp_path / "templates" / templates.key_hash(keep1)).exists()
    assert msg.startswith("dropped 3 template(s), 1 orphan folder(s)")
    assert templates.TASKS[0].name == "prune_db_templates"
