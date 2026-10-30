"""The worker saves a failure diagnosis (Build.diagnosis) on every failure path."""
from pathlib import Path

import pytest

from previewer import diagnose
from previewer.models import Build
from previewer.worker import docker_ops, logs, pipeline

from test_worker_pipeline import _payroll, _spec  # noqa: E402 (tests/ is on sys.path)

FIXTURES = Path(__file__).parent / "fixtures" / "odoo_logs"


@pytest.fixture
def tmp_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "log_path", lambda build_id: tmp_path / "logs" / f"{build_id}.log")


@pytest.fixture
def no_teardown(monkeypatch):
    monkeypatch.setattr(pipeline, "teardown", lambda *a, **k: None)


def _run_with_odoo_output(s, monkeypatch, fixture: str, rc: int = 255, outcome: dict | None = None):
    """run_build where the init container prints a real Odoo failure log."""
    build, bank, _ = _payroll(s)

    def fake_prepare(spec, blog):
        spec.root_is_module = True
        spec.deps[0].sha, spec.deps[0].root_is_module = "b" * 40, True
        spec.deps[1].sha = "c" * 40
        spec.modules = ["payroll_zm"]
        return []

    def fake_init(**kw):
        kw["log"].write((FIXTURES / fixture).read_text())
        if kw.get("outcome") is not None:
            kw["outcome"].update(outcome or {"exit_code": rc, "oom": False, "timed_out": False})
        return rc

    monkeypatch.setattr(pipeline, "_prepare", fake_prepare)
    monkeypatch.setattr(pipeline.docker_ops, "run_init", fake_init)
    pipeline.run_build(build.id, followers=None)
    s.expire_all()
    return s.get(Build, build.id)


def test_odoo_xml_failure_is_diagnosed_and_headlined(s, tmp_logs, no_teardown, monkeypatch):
    b = _run_with_odoo_output(s, monkeypatch, "18.0-diag_xml_syntax.log")
    assert b.status == "failed"
    f = b.diagnosis[0]
    assert f["kind"] == "xml_syntax" and f["line"] == 6 and "tag mismatch" in f["message"]
    assert b.error_summary.startswith("XML syntax error")
    assert len(b.error_summary) <= pipeline.SUMMARY_CAP
    assert "Why it failed: XML syntax error" in logs.read_log(b.id)


def test_oom_signal_from_init_container(s, tmp_logs, no_teardown, monkeypatch):
    b = _run_with_odoo_output(s, monkeypatch, "18.0-diag_py_syntax.log", rc=137,
                              outcome={"exit_code": 137, "oom": True, "timed_out": False})
    assert b.diagnosis[0]["kind"] == "out_of_memory" and "2g" in b.diagnosis[0]["message"]
    assert b.diagnosis[1]["kind"] == "python_syntax"


def test_timeout_signal(s, tmp_logs, no_teardown, monkeypatch):
    b = _run_with_odoo_output(s, monkeypatch, "18.0-diag_xmlid.log", rc=-1,
                              outcome={"exit_code": -1, "oom": False, "timed_out": True})
    assert b.diagnosis[0]["kind"] == "timeout"


def test_setup_failure_has_a_hint(s, tmp_logs, no_teardown, monkeypatch):
    build, _, _ = _payroll(s)

    def boom(spec, blog):
        raise pipeline.BuildFailed("Odoo Enterprise 19.0: x/y has no 19.0 branch (credential 'Default')")

    monkeypatch.setattr(pipeline, "_prepare", boom)
    pipeline.run_build(build.id, followers=None)
    s.expire_all()
    b = s.get(Build, build.id)
    f = b.diagnosis[0]
    assert f["kind"] == "setup" and "Odoo versions" in f["hint"]
    assert b.error_summary.startswith("Build setup failed: Odoo Enterprise 19.0")


def test_missing_dependency_kind(s, tmp_logs, no_teardown, monkeypatch):
    build, _, _ = _payroll(s)

    def missing(spec, blog):
        raise pipeline.BuildFailed("payroll_zm cannot install: it depends on hr_payroll",
                                   kind="missing_dependency")

    monkeypatch.setattr(pipeline, "_prepare", missing)
    pipeline.run_build(build.id, followers=None)
    s.expire_all()
    f = s.get(Build, build.id).diagnosis[0]
    assert f["kind"] == "missing_dependency" and "hr_payroll" in f["message"]


def test_health_failure_reports_oom(s, tmp_logs, no_teardown, monkeypatch):
    build, _, _ = _payroll(s)
    monkeypatch.setattr(pipeline, "_prepare", lambda spec, blog: [])
    monkeypatch.setattr(pipeline, "_init_database", lambda spec, m, blog: None)

    class Ctr:
        id = "c1"
    monkeypatch.setattr(pipeline.docker_ops, "run_preview", lambda **kw: Ctr())
    monkeypatch.setattr(pipeline.docker_ops, "wait_healthy", lambda name, t: "container exited (code 137)")
    monkeypatch.setattr(pipeline.docker_ops, "container_state", lambda name: {"oom": True, "exit_code": 137})

    class Followers:
        def start(self, *a, **k):
            pass
    pipeline.run_build(build.id, followers=Followers())
    s.expire_all()
    kinds = [f["kind"] for f in s.get(Build, build.id).diagnosis]
    assert kinds[0] == "out_of_memory"


def test_mounts_map_root_module_and_multi_module_repos():
    spec = _spec(repo_url="https://github.com/a/payroll_extension", root_module="payroll_zm",
                 sha="a" * 40, enterprise=True, enterprise_repo="https://github.com/ISWE-Solutions/enterprise_addons.git",
                 enterprise_branch="19.0")
    spec.branch_name, spec.root_is_module = "v19", True
    spec.deps = [pipeline.DepSpec(1, "bank_zm", "https://github.com/a/bank_extension.git", "v19", "project",
                                  "t", "acct", sha="b" * 40, root_is_module=True),
                 pipeline.DepSpec(2, "oca_tools", "https://github.com/OCA/tools", "19.0", "default",
                                  "t", "acct", sha="c" * 40, root_is_module=False)]
    m = pipeline.diagnosis_mounts(spec)
    log = ('  File "/mnt/extra/0/bank_zm/views/bank.xml", line 4\n'
           "lxml.etree.XMLSyntaxError: Premature end of data, line 4, column 1\n")
    f = diagnose.diagnose(log, m)[0]
    assert (f.file, f.source) == ("views/bank.xml", "a/bank_extension @ v19")
    assert f.url == "https://github.com/a/bank_extension/blob/" + "b" * 40 + "/views/bank.xml#L4"
    log2 = '  File "/mnt/extra/1/tool_x/models/m.py", line 9\nSyntaxError: invalid syntax\n'
    f2 = diagnose.diagnose(log2, m)[0]
    assert f2.file == "tool_x/models/m.py" and f2.url.endswith("/blob/" + "c" * 40 + "/tool_x/models/m.py#L9")
    log3 = '  File "/mnt/src/payroll_zm/models/p.py", line 2\nSyntaxError: invalid syntax\n'
    f3 = diagnose.diagnose(log3, m)[0]
    assert f3.file == "models/p.py" and f3.source == "a/payroll_extension @ v19"
    assert m[docker_ops.ENTERPRISE].label == "Odoo Enterprise ISWE-Solutions/enterprise_addons @ 19.0"


def test_with_headline_caps_length():
    findings = [diagnose.Finding("xml_syntax", "XML syntax error", "boom", "fix it")]
    out = pipeline.with_headline(findings, "x" * 5000)
    assert out.startswith("XML syntax error: boom") and len(out) <= pipeline.SUMMARY_CAP
