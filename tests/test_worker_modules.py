"""Install planning in the worker: plan saved, early failure, module sets, log feedback."""
import json
from pathlib import Path

import pytest

from previewer.models import Branch, Build, GithubCredential, ModuleSet, Project, ProjectDependency
from previewer.security import encrypt
from previewer.worker import docker_ops, logs, pipeline

CORE = {"base", "hr", "account", "mail"}


def _module(root: Path, name: str, depends=()):
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "__manifest__.py").write_text(repr({"name": name, "depends": list(depends)}))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "log_path", lambda build_id: tmp_path / "logs" / f"{build_id}.log")
    monkeypatch.setattr(pipeline, "build_dir", lambda bid: tmp_path / "b")
    monkeypatch.setattr(pipeline, "filestore_dir", lambda bid: tmp_path / "fs")
    monkeypatch.setattr(pipeline.docker_ops, "ensure_image", lambda img, v, req, blog: img)
    monkeypatch.setattr(pipeline.docker_ops, "core_modules", lambda image: CORE)
    return tmp_path


def _project(s, *, edition="community", combo=False, set_modules=None):
    s.add(GithubCredential(name="Default", token_enc=encrypt("tok"), is_default=True))
    p = Project(name="Payroll", slug="payroll", repo_url="" if combo else "https://github.com/a/payroll",
                kind="combo" if combo else "repo", default_odoo_version="18.0", edition=edition)
    bank = ProjectDependency(project=p, repo_url="https://github.com/a/bank_extension.git",
                             module_name="bank_zm", branch_map={"18.0": "main"})
    b = Branch(project=p, name="v18", slug="v18", host="payroll-v18.preview.localhost",
               last_commit_sha="a" * 40)
    s.add_all([p, bank, b])
    if set_modules:
        ms = ModuleSet(project=p, name="Core", modules=set_modules)
        s.add(ms)
        s.flush()
        b.module_set = ms
    build = Build(branch=b, commit_sha="a" * 40, odoo_version="18.0", status="queued")
    s.add(build)
    s.commit()
    return build


def _fake_code(monkeypatch, repo_modules: dict, bank_modules: dict):
    """Fake fetches: the project repo exports `repo_modules`, the bank dep `bank_modules`."""
    def fake_fetch(path, url, token):
        pass

    def fake_export(mirror, sha, dest):
        for name, depends in repo_modules.items():
            _module(dest, name, depends)

    monkeypatch.setattr(pipeline.gitmirror, "fetch_mirror", fake_fetch)
    monkeypatch.setattr(pipeline.gitmirror, "has_commit", lambda m, sha: True)
    monkeypatch.setattr(pipeline.gitmirror, "export", fake_export)

    def fake_deps(spec_, bdir, blog):
        for name, depends in bank_modules.items():
            _module(bdir / "extra" / "0", name, depends)
    monkeypatch.setattr(pipeline, "_fetch_dependencies", fake_deps)


REPO = {
    "pay_base": ["hr", "bank_zm"],
    "pay_rules": ["pay_base"],
    "pay_reports": ["pay_rules", "account"],
    "pay_portal": ["pay_base", "mail"],
}


def test_plan_saved_and_logged(s, env, monkeypatch):
    build = _project(s)
    _fake_code(monkeypatch, REPO, {"bank_zm": ["base"]})
    spec = pipeline.load_spec(build.id)
    pipeline._prepare(spec, logs.BuildLog(build.id))
    s.expire_all()
    plan = s.get(Build, build.id).install_plan
    assert plan["requested"] == sorted(REPO)
    assert set(plan["install"]) == set(REPO) | {"bank_zm"}
    assert plan["top_level"] == ["pay_portal", "pay_reports"]
    assert plan["sources"]["bank_zm"] == "bank_extension @ main"
    assert plan["sources"]["pay_base"] == "Payroll repo"
    assert spec.modules == sorted(REPO)  # -i list = requested
    assert "Install plan: asked for pay_base, pay_portal, pay_reports, pay_rules -> installs" in \
        logs.read_log(build.id)


def test_module_set_installs_top_level_and_pulls_the_rest(s, env, monkeypatch):
    build = _project(s, set_modules="pay_reports, pay_portal")
    _fake_code(monkeypatch, REPO, {"bank_zm": ["base"]})
    spec = pipeline.load_spec(build.id)
    pipeline._prepare(spec, logs.BuildLog(build.id))
    assert spec.modules == ["pay_reports", "pay_portal"]
    assert set(spec.install) == set(REPO) | {"bank_zm"}
    assert pipeline.init_args(spec)[3] == "pay_reports,pay_portal"


def test_missing_dependency_fails_before_odoo(s, env, monkeypatch):
    build = _project(s)
    _fake_code(monkeypatch, {"pay_base": ["hr", "bank_zm", "hr_payroll"]}, {})
    called = []
    monkeypatch.setattr(pipeline.docker_ops, "run_init", lambda **kw: called.append(kw) or 0)
    monkeypatch.setattr(pipeline, "_enterprise_names", lambda spec, d: {"hr_payroll"})
    spec = pipeline.load_spec(build.id)
    with pytest.raises(pipeline.BuildFailed) as e:
        pipeline._prepare(spec, logs.BuildLog(build.id))
    msg = str(e.value)
    assert "pay_base cannot install: it depends on" in msg
    assert "bank_zm, which is not in the repo" in msg
    assert "hr_payroll (an Odoo Enterprise module: set the edition to Enterprise)" in msg
    assert not called
    s.expire_all()
    assert s.get(Build, build.id).install_plan["missing"] == {"pay_base": ["bank_zm", "hr_payroll"]}


def test_combo_requests_all_parts(s, env, monkeypatch):
    build = _project(s, combo=True)
    _fake_code(monkeypatch, {}, {"bank_zm": ["base"], "bank_extra": ["bank_zm"]})
    spec = pipeline.load_spec(build.id)
    pipeline._prepare(spec, logs.BuildLog(build.id))
    assert spec.modules == ["bank_extra", "bank_zm"]
    s.expire_all()
    assert s.get(Build, build.id).install_plan["top_level"] == ["bank_extra"]


def test_failure_summary_puts_dependency_errors_first():
    log = ("\n=== step\n2026-10-30 10:00:00,000 7 INFO demo odoo.modules.graph: module payroll_zm: "
           "Unmet dependencies: hr_payroll\n"
           "2026-10-30 10:00:01,000 7 ERROR demo odoo.modules.loading: Some modules are not loaded, "
           "some dependencies or manifest may be missing: ['payroll_zm']\n")
    out = pipeline.failure_summary(log, "Modules not installed: payroll_zm (not found)")
    lines = out.splitlines()
    assert lines[0] == "payroll_zm was not installed: Odoo could not find its dependencies hr_payroll"
    assert "Odoo did not load payroll_zm" in lines[1]
    assert "Modules not installed" in out and "ERROR demo odoo.modules.loading" in out


def test_init_verification_uses_odoo_reasons(s, env, monkeypatch):
    spec = pipeline.BuildSpec(
        build_id=99, branch_id=1, project_id=1, host="h", sha="a" * 40, version="18.0",
        base_image="odoo:18", demo_flags=[], repo_url="u", token="", account="", root_module="x",
        enterprise=False, modules=["payroll_zm"], deps=[], enterprise_repo="", enterprise_branch="",
        enterprise_token="", enterprise_account="", db_name="d", mem_limit="1g",
        build_mem_limit="2g", cpus=1, timeout_s=60, health_timeout_s=60,
        install=["payroll_zm", "bank_zm"])
    blog = logs.BuildLog(99)
    blog.write("2026-10-30 10:00:00,000 7 INFO d odoo.modules.graph: module payroll_zm: "
               "Unmet dependencies: hr_payroll\n")
    monkeypatch.setattr(pipeline.docker_ops, "run_init", lambda **kw: 0)
    asked = []
    monkeypatch.setattr(pipeline.pg, "module_states",
                        lambda db, mods: asked.append(mods) or {"bank_zm": "installed"})
    with pytest.raises(pipeline.BuildFailed, match="could not find its dependencies hr_payroll"):
        pipeline._init_database(spec, [], blog)
    assert asked == [["payroll_zm", "bank_zm"]]


def test_core_modules_cached_per_image(tmp_path, monkeypatch):
    monkeypatch.setattr(docker_ops, "get_settings",
                        lambda: type("C", (), {"data_dir": tmp_path})())

    class Img:
        id = "sha256:" + "f" * 64

    class Containers:
        runs = 0

        def run(self, image, **kw):
            Containers.runs += 1
            return json.dumps(["base", "hr"]).encode()

    class Client:
        images = type("I", (), {"get": staticmethod(lambda name: Img())})()
        containers = Containers()

    monkeypatch.setattr(docker_ops, "client", lambda: Client())
    assert docker_ops.core_modules("odoo:18") == {"base", "hr"}
    assert docker_ops.core_modules("odoo:18") == {"base", "hr"}
    assert Containers.runs == 1
    assert (tmp_path / "cache" / ("core-" + "f" * 16 + ".json")).exists()
