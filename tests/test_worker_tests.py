"""Module tests in the build: flags, warn/fail handling, results saved (real Odoo logs)."""
from pathlib import Path

import pytest
from test_worker_modules import REPO, _fake_code, _project, env  # noqa: F401  (fixture)

from previewer.models import Build
from previewer.worker import logs, pipeline

LOGS = Path(__file__).parent / "fixtures" / "odoo_logs"


def _prepared(s, monkeypatch, **project):
    build = _project(s, combo=project.pop("combo", False))
    p = s.get(Build, build.id).branch.project
    for k, v in project.items():
        setattr(p, k, v)
    s.commit()
    _fake_code(monkeypatch, REPO, {"bank_zm": ["base"]})
    spec = pipeline.load_spec(build.id)
    pipeline._prepare(spec, logs.BuildLog(build.id))
    return build, spec


def _odoo(monkeypatch, log_file: str, rc: int, installed=True):
    """Fake init container: writes a real Odoo log and exits with rc."""
    def run_init(**kw):
        kw["log"].write((LOGS / log_file).read_text())
        return rc
    monkeypatch.setattr(pipeline.docker_ops, "run_init", run_init)
    monkeypatch.setattr(pipeline, "_from_template", lambda spec, blog: False)
    monkeypatch.setattr(pipeline.pg, "module_states",
                        lambda db, mods: {m: "installed" if installed else "to install" for m in mods})


def test_default_tags_are_the_own_modules(s, env, monkeypatch):
    _, spec = _prepared(s, monkeypatch)
    args = pipeline.init_args(spec)
    assert args[args.index("--test-tags") + 1] == ",".join(f"/{m}" for m in spec.own_modules)
    assert set(spec.own_modules) == set(REPO) and "bank_zm" not in spec.own_modules
    assert "--test-enable" in args


def test_custom_tags_and_tests_off(s, env, monkeypatch):
    _, spec = _prepared(s, monkeypatch, test_tags="/pay_base:TestPay")
    assert pipeline.init_args(spec)[-1] == "/pay_base:TestPay"
    spec.run_tests = False
    assert "--test-enable" not in pipeline.init_args(spec)


def test_branch_override_off(s, env, monkeypatch):
    build = _project(s)
    b = s.get(Build, build.id).branch
    b.run_tests_override = False
    s.commit()
    assert pipeline.load_spec(build.id).run_tests is False


def test_combo_tests_all_parts(s, env, monkeypatch):
    _, spec = _prepared(s, monkeypatch, combo=True)
    assert spec.own_modules == ["bank_zm"]
    assert pipeline.init_args(spec)[-1] == "/bank_zm"


def test_plain_previews_skip_tests(s, env, monkeypatch):
    _, spec = _prepared(s, monkeypatch)
    spec.plain = True
    _odoo(monkeypatch, "19.0-tests-pass.log", 0)
    pipeline._init_database(spec, [], logs.BuildLog(spec.build_id))
    s.expire_all()
    b = s.get(Build, spec.build_id)
    assert (b.test_status, b.test_results) == ("skipped", {})
    assert "--test-enable" not in pipeline.init_args(spec)


def test_passing_run_saved(s, env, monkeypatch):
    build, spec = _prepared(s, monkeypatch)
    _odoo(monkeypatch, "19.0-tests-pass.log", 0)
    pipeline._init_database(spec, [], logs.BuildLog(build.id))
    s.expire_all()
    b = s.get(Build, build.id)
    assert b.test_status == "passed" and b.test_results["headline"] == "2 test(s) passed"
    assert "Tests: 2 test(s) passed" in logs.read_log(build.id)


def test_failed_tests_warn_mode_keeps_going(s, env, monkeypatch):
    build, spec = _prepared(s, monkeypatch)
    _odoo(monkeypatch, "19.0-tests.log", 1)
    pipeline._init_database(spec, [], logs.BuildLog(build.id))  # no exception
    s.expire_all()
    b = s.get(Build, build.id)
    assert b.test_status == "failed" and b.test_results["failed"] == 1
    assert "Tests failed (warn mode): 1 failed and 1 error(s) of 6 tests" in logs.read_log(build.id)


def test_failed_tests_fail_mode_fails_with_a_tests_finding(s, env, monkeypatch):
    build, spec = _prepared(s, monkeypatch, test_failure_mode="fail")
    _odoo(monkeypatch, "19.0-tests.log", 1)
    with pytest.raises(pipeline.BuildFailed) as e:
        pipeline._init_database(spec, [], logs.BuildLog(build.id))
    assert e.value.kind == "tests"
    [f] = pipeline.build_diagnosis(spec, e.value, logs.read_log(build.id))
    assert f.kind == "tests" and f.title.startswith("Module tests failed: 1 failed")
    assert f.module == "diag_tests" and f.line in (12, 15) and "When tests fail" in f.hint
    s.expire_all()
    assert s.get(Build, build.id).test_status == "failed"


def test_exit_1_without_tests_is_an_install_failure(s, env, monkeypatch):
    build, spec = _prepared(s, monkeypatch)
    _odoo(monkeypatch, "19.0-diag_xml_syntax.log", 255)
    with pytest.raises(pipeline.BuildFailed) as e:
        pipeline._init_database(spec, [], logs.BuildLog(build.id))
    assert e.value.kind == "odoo"
    s.expire_all()
    assert s.get(Build, build.id).test_status is None  # tests on, but none ran


def test_failed_tests_with_modules_not_installed_is_an_install_failure(s, env, monkeypatch):
    build, spec = _prepared(s, monkeypatch)
    _odoo(monkeypatch, "19.0-tests.log", 1, installed=False)
    with pytest.raises(pipeline.BuildFailed) as e:
        pipeline._init_database(spec, [], logs.BuildLog(build.id))
    assert e.value.kind == "odoo"
    s.expire_all()
    assert s.get(Build, build.id).test_status == "failed"  # results kept anyway


def test_timeout_hint_mentions_tests(s, env, monkeypatch):
    _, spec = _prepared(s, monkeypatch)
    err = pipeline.BuildFailed("Module installation failed (exit code 137)", kind="odoo",
                               signals=pipeline.diagnose.Signals(timed_out=True, timeout_minutes=20))
    [f] = pipeline.build_diagnosis(spec, err, "")
    assert f.kind == "timeout" and "Module tests are on" in f.hint
