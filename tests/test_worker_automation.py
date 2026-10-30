"""Phase 2 automation: poll, visit tracking, sleep on idle, running limit, disk guard."""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from previewer import appsettings
from previewer.models import Branch, Build, Job, Project, utcnow
from previewer.worker import automation, lifecycle, logs


@pytest.fixture(autouse=True)
def quiet_docker(tmp_path, monkeypatch):
    """No real containers or live build logs; record which containers were stopped."""
    stopped: list[str] = []
    monkeypatch.setattr(logs, "log_path", lambda build_id: tmp_path / "logs" / f"{build_id}.log")
    monkeypatch.setattr(lifecycle.docker_ops, "stop_container", lambda name: stopped.append(name))
    return stopped


def _branch(s, slug="bank", name="v19", host=None):
    p = s.query(Project).filter_by(slug=slug).first()
    if p is None:
        p = Project(name=slug.title(), slug=slug, repo_url=f"https://github.com/acme/{slug}",
                    default_odoo_version="19.0")
        s.add(p)
    b = Branch(project=p, name=name, slug=name, host=host or f"{slug}-{name}.preview.localhost",
               last_commit_sha="a" * 40)
    s.add(b)
    s.commit()
    return b


def _build(s, branch, status="running", idle=timedelta(0)):
    b = Build(branch_id=branch.id, commit_sha="a" * 40, odoo_version="19.0", status=status,
              host=branch.host, finished_at=utcnow() - idle, last_accessed_at=utcnow() - idle)
    s.add(b)
    s.commit()
    return b


def test_poll_enqueues_one_sync_per_project(s):
    _branch(s, "bank")
    _branch(s, "payroll")
    assert automation.poll() == "queued 2 sync job(s)"
    assert automation.poll() == "queued 0 sync job(s)"  # still pending: no duplicates
    assert s.query(Job).filter_by(type="sync").count() == 2


def _access(path, *records):
    with open(path, "a") as f:
        for host, t in records:
            f.write(json.dumps({"RequestHost": host, "time": t, "DownstreamStatus": 200}) + "\n")


def test_access_log_tailing_keeps_offset_and_handles_rotation(tmp_path):
    log, state = tmp_path / "access.log", tmp_path / "offset"
    _access(log, ("bank-v19.preview.localhost", "2026-10-30T10:00:00Z"),
            ("bank-v19.preview.localhost:80", "2026-10-30T10:05:00Z"),
            ("preview.localhost", "2026-10-30T10:01:00Z"))
    seen = automation.read_new_visits(log, state)
    assert seen["bank-v19.preview.localhost"].minute == 5 and "preview.localhost" in seen
    assert automation.read_new_visits(log, state) == {}  # nothing new
    with open(log, "a") as f:
        f.write('{"RequestHost": "half-written')  # incomplete line is left for next time
    assert automation.read_new_visits(log, state) == {}
    with open(log, "a") as f:
        f.write('.preview.localhost", "time": "2026-10-30T11:00:00Z"}\n')
    assert list(automation.read_new_visits(log, state)) == ["half-written.preview.localhost"]
    log.unlink()  # rotated: a new file starts from the beginning
    _access(log, ("x.preview.localhost", "2026-10-30T12:00:00Z"))
    assert list(automation.read_new_visits(log, state)) == ["x.preview.localhost"]


def test_visits_update_live_build_at_most_once_a_minute(s):
    br = _branch(s)
    build = _build(s, br, idle=timedelta(hours=1))
    t = utcnow()
    assert automation.record_visits({br.host: t}) == 1
    assert automation.record_visits({br.host: t + timedelta(seconds=20)}) == 0
    assert automation.record_visits({br.host: t + timedelta(minutes=2)}) == 1
    s.expire_all()
    assert s.get(Build, build.id).last_accessed_at == t + timedelta(minutes=2)


def test_sleep_after_idle_hours_and_wake_resets(s, monkeypatch, quiet_docker):
    with s.begin_nested():
        appsettings.put(s, "idle_sleep_hours", "3")
    s.commit()
    idle = _build(s, _branch(s, name="old"), idle=timedelta(hours=4))
    fresh = _build(s, _branch(s, name="new"), idle=timedelta(minutes=10))
    assert automation.sleep_idle().startswith("slept 1 idle")
    s.expire_all()
    assert s.get(Build, idle.id).status == "sleeping" and s.get(Build, fresh.id).status == "running"
    assert quiet_docker == [f"pv-{idle.id}"]

    ctr = SimpleNamespace(status="exited", start=lambda: None)
    monkeypatch.setattr(lifecycle.docker_ops, "get_container", lambda name: ctr)
    monkeypatch.setattr(lifecycle.docker_ops, "wait_healthy", lambda name, t: None)
    lifecycle.start(idle.id, SimpleNamespace(start=lambda build_id: None))
    s.expire_all()
    woke = s.get(Build, idle.id)
    assert woke.status == "running" and utcnow() - woke.last_accessed_at < timedelta(minutes=1)
    assert "Preview woke up" in logs.read_log(idle.id)


def test_running_limit_sleeps_longest_idle(s, quiet_docker):
    with s.begin_nested():
        appsettings.put(s, "max_running_previews", "2")
    s.commit()
    a = _build(s, _branch(s, name="a"), idle=timedelta(hours=2))
    b = _build(s, _branch(s, name="b"), idle=timedelta(minutes=5))
    c = _build(s, _branch(s, name="c"), idle=timedelta(hours=1))
    assert automation.enforce_running_limit() == [a.id]
    assert automation.enforce_running_limit(exclude=b.id) == []
    s.expire_all()
    assert [s.get(Build, x.id).status for x in (a, b, c)] == ["sleeping", "running", "running"]


def test_disk_guard_holds_queued_builds(s, monkeypatch):
    br = _branch(s)
    build = _build(s, br, status="queued")
    s.add(Job(type="build", payload={"build_id": build.id}, branch_id=br.id, build_id=build.id))
    s.commit()
    monkeypatch.setattr(automation, "disk_status", lambda: {"free_gb": 2.0, "total_gb": 200.0, "percent_used": 99.0})
    assert "held 1 build" in automation.disk_guard()
    s.expire_all()
    job = s.query(Job).one()
    assert job.run_after > utcnow() + timedelta(minutes=4)
    assert "Waiting for disk space: 2.0 GB free, need 5 GB" in logs.read_log(build.id)
    monkeypatch.setattr(automation, "disk_status", lambda: {"free_gb": 50.0, "total_gb": 200.0, "percent_used": 75.0})
    assert automation.disk_guard() == "50.0 GB free"


def test_cpu_percent_from_two_samples():
    sample = lambda total, system: {"cpu_stats": {"cpu_usage": {"total_usage": total},
                                                  "system_cpu_usage": system, "online_cpus": 4}}
    automation._prev_cpu.clear()
    assert automation._cpu_percent(1, sample(100, 1000)) is None
    assert automation._cpu_percent(1, sample(150, 1100)) == 200.0
