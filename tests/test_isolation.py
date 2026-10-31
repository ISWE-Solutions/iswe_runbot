"""Isolation and capacity: own Postgres role per build, preview network, memory-fitted running
limit, start-up check, webhook signatures, required SECRET_KEY."""
import hashlib
import hmac
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from previewer import appsettings, capacity
from previewer.config import Settings, get_settings
from previewer.models import Build, Job, Project, ProjectDependency
from previewer.web import system_view
from previewer.worker import automation, docker_ops, hardening, lifecycle, logs, pipeline

from test_web_app import client, make_project  # noqa: F401
from test_worker_automation import _branch, _build

GB = 1024**3


@pytest.fixture(autouse=True)
def tmp_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "log_path", lambda build_id: tmp_path / "logs" / f"{build_id}.log")
    automation._host_cache.clear()
    yield
    automation._host_cache.clear()


# ---------- capacity ----------

def test_parse_memory():
    assert capacity.parse_memory("1536m") == 1536 * 1024**2
    assert capacity.parse_memory("2g") == 2 * GB and capacity.parse_memory("1.5G") == int(1.5 * GB)
    for bad in ("", "0x", "2gb", "-1g"):
        with pytest.raises(ValueError):
            capacity.parse_memory(bad)


def test_fit_on_an_8_gb_docker_host():
    """The review's numbers: 25 x 1.5 GB previews and 2 x 2 GB builds on 8.3 GB."""
    f = capacity.fit(total=int(8.3e9), previews=25, preview_mem=capacity.parse_memory("1536m"), builds=2,
                     build_mem=2 * GB, reserved=2 * GB)
    assert f.max_previews == 1 and not f.memory_ok
    f2 = capacity.fit(total=int(8.3e9), previews=4, preview_mem=capacity.parse_memory("768m"), builds=1,
                      build_mem=2 * GB, reserved=2 * GB, overcommit=1.0)
    assert f2.max_previews == 4
    assert capacity.fit(total=int(8.3e9), previews=25, preview_mem=capacity.parse_memory("1536m"), builds=2,
                        build_mem=2 * GB, reserved=2 * GB, overcommit=2).max_previews == 6


def test_fit_respects_postgres_connections():
    f = capacity.fit(total=512 * GB, previews=25, preview_mem=GB, builds=3, build_mem=GB, reserved=GB,
                     maxconn=12, max_connections=300)
    assert f.max_previews == (300 - 20) // 12 - 3 and not f.connections_ok


def test_effective_limit_unknown_host_keeps_setting():
    assert capacity.effective_limit(25, None, preview_mem="1536m", builds=2, build_mem="2g", reserved="2g") == 25
    assert capacity.effective_limit(25, int(8.3e9), preview_mem="1536m", builds=2, build_mem="2g",
                                    reserved="2g") == 1


# ---------- System page ----------

def _form(**kw):
    current = appsettings.defaults()
    form = {k: v for k, v in current.items() if not k.endswith("_enc")}
    form["use_db_templates"] = "on"
    form.update(kw)
    return form, current


def test_settings_refuse_more_previews_than_memory_allows(monkeypatch):
    monkeypatch.setattr(system_view, "heartbeat", lambda: {"docker": {"mem_total_gb": 8.3}})
    form, current = _form(max_running_previews="25", max_concurrent_builds="2")
    _, errors = system_view.validate(form, current)
    assert "Only 1 fit" in errors["max_running_previews"]
    form, current = _form(max_running_previews="4", max_concurrent_builds="1", preview_memory_limit="768m")
    assert system_view.validate(form, current)[1] == {}


def test_settings_reject_zero_memory_and_zero_previews(monkeypatch):
    monkeypatch.setattr(system_view, "heartbeat", lambda: {})
    form, current = _form(preview_memory_limit="0", build_memory_limit="0m", max_running_previews="0",
                          memory_overcommit="9")
    errors = system_view.validate(form, current)[1]
    assert set(errors) >= {"preview_memory_limit", "build_memory_limit", "max_running_previews", "memory_overcommit"}


# ---------- running limit ----------

class FakeCtr(SimpleNamespace):
    pass


def test_running_limit_counts_starting_previews_and_stops_orphans(s, monkeypatch):
    stopped = []
    monkeypatch.setattr(docker_ops, "stop_container", lambda name: stopped.append(name))
    with s.begin_nested():
        appsettings.put(s, "max_running_previews", "2")
    s.commit()
    a = _build(s, _branch(s, name="a"), idle=timedelta(hours=2))
    b = _build(s, _branch(s, name="b"), idle=timedelta(minutes=1))
    starting = _build(s, _branch(s, name="c"), status="building")
    failed_wake = _build(s, _branch(s, name="d"), status="sleeping")
    live = {x.id: FakeCtr() for x in (a, b, starting, failed_wake)}
    monkeypatch.setattr(automation, "_live_containers", lambda: live)
    monkeypatch.setattr(automation, "host_memory", lambda: None)
    assert automation.enforce_running_limit() == [a.id]
    assert f"pv-{failed_wake.id}" in stopped
    s.expire_all()
    assert s.get(Build, a.id).status == "sleeping"


def test_waking_preview_is_not_an_orphan(s, monkeypatch):
    stopped = []
    monkeypatch.setattr(docker_ops, "stop_container", lambda name: stopped.append(name))
    waking = _build(s, _branch(s, name="w"), status="sleeping")
    s.add(Job(type="wake", payload={"build_id": waking.id}, status="running"))
    s.commit()
    monkeypatch.setattr(automation, "_live_containers", lambda: {waking.id: FakeCtr()})
    monkeypatch.setattr(automation, "host_memory", lambda: None)
    automation.enforce_running_limit()
    assert stopped == []


def test_running_limit_is_capped_to_host_memory(s, monkeypatch):
    stopped = []
    monkeypatch.setattr(docker_ops, "stop_container", lambda name: stopped.append(name))
    with s.begin_nested():
        appsettings.put(s, "max_running_previews", "25")
        appsettings.put(s, "max_concurrent_builds", "2")
    s.commit()
    monkeypatch.setattr(automation, "host_memory", lambda: int(8.3e9))
    monkeypatch.setattr(automation.pg, "max_connections", lambda: 300)
    assert automation.running_limit() == 1
    with s.begin_nested():
        appsettings.put(s, "memory_overcommit", "2")
    s.commit()
    assert automation.running_limit() == 6


def test_make_room_before_start_spares_the_branchs_live_preview(s, monkeypatch):
    monkeypatch.setattr(docker_ops, "stop_container", lambda name: None)
    with s.begin_nested():
        appsettings.put(s, "max_running_previews", "2")
    s.commit()
    br = _branch(s, name="main")
    live_same_branch = _build(s, br, idle=timedelta(hours=5))
    other = _build(s, _branch(s, name="x"), idle=timedelta(hours=1))
    new = _build(s, br, status="building")
    monkeypatch.setattr(automation, "_live_containers",
                        lambda: {live_same_branch.id: FakeCtr(), other.id: FakeCtr()})
    monkeypatch.setattr(automation, "host_memory", lambda: None)
    assert automation.make_room(new.id) == [other.id]


def test_failed_wake_stops_the_container_again(s, monkeypatch):
    stopped = []
    b = _build(s, _branch(s, name="z"), status="sleeping")
    ctr = SimpleNamespace(status="exited", start=lambda: None)
    monkeypatch.setattr(docker_ops, "get_container", lambda name: ctr)
    monkeypatch.setattr(docker_ops, "stop_container", lambda name: stopped.append(name))
    monkeypatch.setattr(docker_ops, "wait_healthy", lambda name, t: "container keeps crashing (exit code 1)")
    monkeypatch.setattr(automation, "_live_containers", lambda: None)
    monkeypatch.setattr(automation, "host_memory", lambda: None)
    with pytest.raises(lifecycle.JobError):
        lifecycle.start(b.id, SimpleNamespace(start=lambda build_id: None))
    assert stopped == [f"pv-{b.id}"]
    s.expire_all()
    assert s.get(Build, b.id).status == "sleeping"


# ---------- start-up check ----------

def _ctr(status="running", restarts=0, health="starting", exit_code=0, oom=False):
    return SimpleNamespace(status=status, attrs={
        "RestartCount": restarts, "State": {"ExitCode": exit_code, "OOMKilled": oom, "Health": {"Status": health}}})


def test_crash_loop_fails_fast(monkeypatch):
    monkeypatch.setattr(docker_ops, "client", lambda: SimpleNamespace(
        containers=SimpleNamespace(get=lambda name: _ctr(status="restarting", restarts=2, exit_code=1))))
    problem = docker_ops.wait_healthy("pv-1", 600)
    assert problem.startswith("container keeps crashing") and "restarted 2x" in problem
    assert docker_ops.crashed(_ctr(restarts=1, exit_code=137, oom=True)).endswith("out of memory; restarted 1x)")
    assert docker_ops.crashed(_ctr()) is None


def test_slow_start_gets_one_more_wait(monkeypatch):
    calls = []

    def wait(name, t):
        calls.append(t)
        return None if len(calls) == 2 else f"{docker_ops.TIMED_OUT} (/web/login unreachable) after {t}s"
    monkeypatch.setattr(docker_ops, "wait_healthy", wait)
    monkeypatch.setattr(docker_ops, "get_container", lambda name: _ctr())
    assert pipeline.wait_until_healthy("pv-1", 120, logs.BuildLog(1)) is None and calls == [120, 120]
    assert "still starting after 120s" in logs.read_log(1)


def test_crash_is_not_waited_for_again(monkeypatch):
    calls = []
    monkeypatch.setattr(docker_ops, "wait_healthy", lambda name, t: calls.append(t) or "container exited (code 1)")
    assert pipeline.wait_until_healthy("pv-1", 120, logs.BuildLog(1)) == "container exited (code 1)"
    assert calls == [120]


def test_docker_healthcheck_follows_the_time_limit():
    assert docker_ops.healthcheck(300)["start_period"] == 300 * 1_000_000_000
    assert docker_ops.healthcheck(5)["start_period"] == 30 * 1_000_000_000


def test_previews_run_on_the_preview_network(monkeypatch):
    created = {}

    class Containers:
        def create(self, image, **kw):
            created.update(kw)
            return SimpleNamespace(id="c1", start=lambda: created.setdefault("started", True))

        def get(self, name):
            raise docker_ops.NotFound("x")
    monkeypatch.setattr(docker_ops, "client", lambda: SimpleNamespace(containers=Containers()))
    docker_ops.run_preview(image="odoo:19", build_id=9, branch_id=1, host="h.preview.localhost", db_name="d",
                           mounts_=[], mem_limit="1g", cpus=1, health_timeout_s=200)
    assert created["network"] == get_settings().preview_network != get_settings().docker_network
    assert created["labels"]["traefik.docker.network"] == get_settings().preview_network
    assert created["healthcheck"]["start_period"] == 200 * 1_000_000_000 and created["started"]


def test_missing_preview_network_stops_the_worker(monkeypatch):
    def get(name):
        raise docker_ops.NotFound("x")
    monkeypatch.setattr(docker_ops, "client", lambda: SimpleNamespace(networks=SimpleNamespace(get=get)))
    with pytest.raises(RuntimeError, match="previewer_previews is missing"):
        docker_ops.check_preview_network()


# ---------- legacy previews ----------

def test_conf_login_is_replaced():
    conf = "[options]\ndb_user = odoo\ndb_password = old\ndb_name = d\n"
    out = hardening.set_conf_login(conf, "pv_b4", "new", 12)
    assert "db_user = pv_b4" in out and "db_password = new" in out and "db_maxconn = 12" in out
    assert "odoo" not in out and "old" not in out and out.count("db_user") == 1


# ---------- webhook ----------

def _post(path, payload, secret, event="push", raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    if secret is not None:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client().post(path, content=body, headers=headers)


def _payload(url="https://github.com/acme/payroll_extension.git"):
    return {"repository": {"full_name": "acme/x", "clone_url": url}}


def test_unsigned_requests_learn_nothing(s):
    make_project(s, webhook_secret="s3cret")
    known = _post("/api/webhooks/github", _payload(), None)
    unknown = _post("/api/webhooks/github", _payload("https://github.com/acme/secret-repo.git"), None)
    assert known.status_code == unknown.status_code == 401 and known.json() == unknown.json()
    wrong = _post("/api/webhooks/github", _payload("https://github.com/acme/secret-repo.git"), "guess")
    assert wrong.status_code == 401 and "secret-repo" not in wrong.text
    assert _post("/api/webhooks/github", None, None, raw=b"not json").status_code == 401


def test_one_projects_secret_does_not_sync_another(s):
    a = make_project(s, webhook_secret="secret-a")
    combo = Project(kind="combo", name="Suite", slug="suite", repo_url="", default_odoo_version="18.0",
                    webhook_secret="secret-b")
    s.add(combo)
    s.flush()
    s.add(ProjectDependency(project=combo, repo_url=a.repo_url, module_name="payroll_zm"))
    s.commit()
    r = _post("/api/webhooks/github", _payload(), "secret-b")
    assert r.status_code == 200 and r.json()["synced"] == ["suite"]


def test_project_payload_url(s):
    p = make_project(s, webhook_secret="s3cret")
    assert _post(f"/api/webhooks/github/{p.id}", _payload(), "wrong").status_code == 401
    assert _post(f"/api/webhooks/github/{p.id + 99}", _payload(), "s3cret").status_code == 401
    r = _post(f"/api/webhooks/github/{p.id}", _payload(), "s3cret")
    assert r.status_code == 200 and r.json()["synced"] == ["payroll"]
    other = _post(f"/api/webhooks/github/{p.id}", _payload("https://github.com/acme/other.git"), "s3cret")
    assert other.status_code == 202 and "not used by payroll" in other.json()["ignored"]
    assert _post(f"/api/webhooks/github/{p.id}", _payload(), "s3cret", event="ping").json()["message"] == "pong"


def test_oversized_payload_is_refused(s, monkeypatch):
    from previewer.web.routes import webhooks
    monkeypatch.setattr(webhooks, "MAX_BODY", 100)
    p = make_project(s, webhook_secret="s3cret")
    r = _post(f"/api/webhooks/github/{p.id}", {"pad": "x" * 200}, "s3cret")
    assert r.status_code == 413


def test_webhook_secret_is_stored_encrypted(s):
    p = make_project(s, webhook_secret="s3cret")
    s.expire_all()
    row = s.get(Project, p.id)
    assert row.webhook_secret == "s3cret" and "s3cret" not in row.webhook_secret_enc


# ---------- secrets ----------

@pytest.mark.parametrize("key", ["", "dev-secret-change-me", "short"])
def test_weak_secret_key_is_refused(key):
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        Settings(secret_key=key).check_secrets()


def test_strong_secret_key_is_accepted():
    Settings(secret_key="x" * 40).check_secrets()


def test_system_page_shows_the_enforced_limit(s, monkeypatch):
    from test_web_app import login, make_user
    monkeypatch.setattr(system_view, "heartbeat", lambda: {"docker": {"mem_total_gb": 8.3}})
    with s.begin_nested():
        appsettings.put(s, "max_running_previews", "25")
        appsettings.put(s, "max_concurrent_builds", "2")
    s.commit()
    c = client()
    login(c, make_user(s).email)
    page = c.get("/admin/system").text
    assert "running of 1 allowed" in page and "Setting is 25; only 1 fits" in page
