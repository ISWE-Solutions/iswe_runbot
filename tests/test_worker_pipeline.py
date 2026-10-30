import subprocess
from pathlib import Path

import pytest

from previewer.models import Branch, Build, Project, ProjectDependency
from previewer.models import GithubCredential, OdooVersion
from previewer.security import encrypt
from previewer.worker import docker_ops, logs, pipeline
from previewer.worker.logs import error_summary


def _module(root: Path, name: str):
    (root / name).mkdir(parents=True)
    (root / name / "__manifest__.py").write_text("{'name': 'x'}")


def test_detect_modules(tmp_path):
    _module(tmp_path, "b_mod")
    _module(tmp_path, "a_mod")
    (tmp_path / "docs").mkdir()
    assert pipeline.detect_modules(tmp_path) == ["a_mod", "b_mod"]


def test_place_export_root_module(tmp_path):
    export = tmp_path / "repo"
    export.mkdir()
    (export / "__manifest__.py").write_text("{}")
    pipeline.place_export(export, tmp_path / "src", "payroll_zm")
    assert (tmp_path / "src" / "payroll_zm" / "__manifest__.py").exists()
    assert pipeline.detect_modules(tmp_path / "src") == ["payroll_zm"]


def test_place_export_multi_module(tmp_path):
    export = tmp_path / "repo"
    _module(export, "m1")
    pipeline.place_export(export, tmp_path / "src", "ignored")
    assert pipeline.detect_modules(tmp_path / "src") == ["m1"]


def test_split_modules():
    assert pipeline.split_modules(" a, b\nc ,,") == ["a", "b", "c"]
    assert pipeline.split_modules(None) == []


def test_addons_path_and_conf():
    assert docker_ops.addons_path(2, True) == "/mnt/enterprise,/mnt/extra/0,/mnt/extra/1,/mnt/src"
    assert docker_ops.addons_path(0, False) == "/mnt/src"
    conf = docker_ops.render_conf(db_name="p_v18_abc_b1", admin_passwd="s3cret", addons="/mnt/src")
    assert conf.startswith("[options]\n")
    for line in ("dbfilter = ^p_v18_abc_b1$", "list_db = False", "admin_passwd = s3cret",
                 "db_host = postgres", "addons_path = /mnt/src", "proxy_mode = True", "workers = 0"):
        assert line in conf


def test_preview_labels():
    labels = docker_ops.preview_labels(42, 7, "payroll-v18.preview.localhost")
    assert labels["traefik.enable"] == "true"
    assert labels["traefik.http.routers.pv-42.rule"] == "Host(`payroll-v18.preview.localhost`)"
    assert labels["traefik.http.routers.pv-42.priority"] == "10042"
    assert labels["traefik.http.services.pv-42.loadbalancer.server.port"] == "8069"
    assert labels["previewer.build_id"] == "42" and labels["previewer.branch_id"] == "7"


def test_init_args_demo_flags():
    spec = pipeline.BuildSpec(
        build_id=1, branch_id=1, project_id=1, host="h", sha="a" * 40, version="18.0",
        base_image="odoo:18", demo_flags=["--without-demo=all"], repo_url="u", token="t",
        account="a", root_module="m", enterprise=False, modules=["a", "b"], deps=[],
        enterprise_repo="", enterprise_branch="", enterprise_token="", enterprise_account="", db_name="d",
        mem_limit="1g", build_mem_limit="2g", cpus=1, timeout_s=60, health_timeout_s=60)
    assert pipeline.init_args(spec) == ["-d", "d", "-i", "a,b", "--stop-after-init", "--without-demo=all"]


LOG = """=== [2026-10-30 10:00:00] Creating database
2026-10-30 10:00:01,000 1 INFO db odoo.modules.loading: loading 1 modules...
2026-10-30 10:00:02,000 1 ERROR db odoo.modules.registry: Failed to load registry
Traceback (most recent call last):
  File "x.py", line 1, in <module>
ValueError: External ID not found in the system: bank_zm.view_x
2026-10-30 10:00:03,000 1 CRITICAL db odoo.service.server: Failed to initialize database `db`.
2026-10-30 10:00:04,000 1 INFO db odoo: done
"""


def test_error_summary_picks_error_lines_and_exception():
    out = error_summary(LOG, fallback="Module installation failed (exit code 255)")
    lines = out.splitlines()
    assert lines[0] == "Module installation failed (exit code 255)"
    assert "ERROR db odoo.modules.registry" in out
    assert "ValueError: External ID not found" in out
    assert "CRITICAL" in out and "INFO" not in out


def test_error_summary_fallback_only():
    assert error_summary("nothing here", fallback="boom") == "boom"
    assert error_summary("") .startswith("Build failed")


def test_log_lines_resplit():
    chunks = [b"a1\nb", b"2\n", b"c3\nd4\ne", b"5"]
    assert list(docker_ops._lines(chunks)) == [b"a1\n", b"b2\n", b"c3\n", b"d4\n", b"e5"]


@pytest.fixture
def tmp_logs(tmp_path, monkeypatch):
    """Keep test build logs out of the live DATA_DIR (test build ids clash with real ones)."""
    monkeypatch.setattr(logs, "log_path", lambda build_id: tmp_path / "logs" / f"{build_id}.log")


def _payroll(s, edition="community"):
    s.add(GithubCredential(name="Default", username="bot", token_enc=encrypt("tok"), is_default=True))
    bank_acct = GithubCredential(name="Bank reader", token_enc=encrypt("deptok"))
    p = Project(name="Payroll", slug="payroll", repo_url="https://github.com/a/payroll_extension",
                default_odoo_version="18.0", module_name="payroll_zm", edition=edition)
    bank = ProjectDependency(project=p, repo_url="https://github.com/a/bank_extension.git",
                             module_name="bank_zm", branch_map={"18.0": "main"}, credential=bank_acct)
    utils = ProjectDependency(project=p, repo_url="https://github.com/a/utils", position=1)
    b = Branch(project=p, name="v18-astro", slug="v18-astro", host="payroll-v18-astro.preview.localhost",
               last_commit_sha="a" * 40)
    s.add_all([p, bank, utils, b])
    s.flush()
    b.dependency_branches = {str(utils.id): "feature"}
    build = Build(branch=b, commit_sha="a" * 40, odoo_version="18.0", status="queued")
    s.add(build)
    s.commit()
    return build, bank, utils


def test_load_spec_snapshots_dependencies(s, tmp_logs):
    build, bank, utils = _payroll(s)
    spec = pipeline.load_spec(build.id)
    got = [(d.dep_id, d.name, d.branch, d.source, d.token, d.account) for d in spec.deps]
    assert got == [(bank.id, "bank_zm", "main", "project", "deptok", "credential 'Bank reader'"),
                   (utils.id, "utils", "feature", "branch", "tok", "credential 'Default' (bot)")]
    assert (spec.token, spec.account) == ("tok", "credential 'Default' (bot)")
    assert spec.enterprise_repo == "" and spec.enterprise_token == ""


def _bare_repo(tmp_path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=work, check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    (work / "__manifest__.py").write_text("{'name': 'Banks'}")
    run("add", ".")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    bare = tmp_path / "bank.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True, capture_output=True)
    return bare


def test_fetch_dependencies_records_refs_and_fails_clearly(s, tmp_logs, tmp_path, monkeypatch):
    build, bank, utils = _payroll(s)
    bare = _bare_repo(tmp_path)
    monkeypatch.setattr(pipeline.gitmirror, "extra_mirror", lambda url: bare)
    fetched = []
    monkeypatch.setattr(pipeline.gitmirror, "fetch_mirror", lambda path, url, token: fetched.append(token))
    spec = pipeline.BuildSpec(
        build_id=build.id, branch_id=build.branch_id, project_id=1, host="h", sha="a" * 40,
        version="18.0", base_image="odoo:18", demo_flags=[], repo_url="u", token="tok",
        account="credential 'Default'", root_module="payroll_zm", enterprise=False, modules=[],
        enterprise_repo="", enterprise_branch="", enterprise_token="", enterprise_account="", db_name="d",
        deps=[pipeline.DepSpec(bank.id, "bank_zm", bank.repo_url, "main", "project", "deptok", "acct-b"),
              pipeline.DepSpec(utils.id, "utils", utils.repo_url, "nope", "branch", "tok", "acct-d")],
        mem_limit="1g", build_mem_limit="2g", cpus=1, timeout_s=60, health_timeout_s=60)
    bdir = tmp_path / "build"
    with pytest.raises(pipeline.BuildFailed) as e:
        pipeline._fetch_dependencies(spec, bdir, logs.BuildLog(build.id))
    assert str(e.value) == ("Dependency utils: utils has no branch 'nope' "
                            "(branch override, set in branch settings)")
    assert fetched == ["deptok", "tok"]
    assert (bdir / "extra" / "0" / "bank_zm" / "__manifest__.py").exists()
    s.expire_all()
    refs = s.get(Build, build.id).dependency_refs
    assert [(r["name"], r["branch"], r["source"], bool(r["sha"])) for r in refs] == [
        ("bank_zm", "main", "project", True), ("utils", "nope", "branch", False)]
    assert "bank_zm: a/bank_extension @ main" in logs.read_log(build.id)


def test_describe_source():
    assert pipeline.describe_source("project", "18.0") == "project mapping for 18.0"
    assert pipeline.describe_source("default", "19.0") == "default: branch named after Odoo 19.0"


def _spec(**kw):
    base = dict(build_id=1, branch_id=1, project_id=1, host="h", sha="a" * 40, version="19.0",
                base_image="odoo:19", demo_flags=[], repo_url="https://github.com/a/payroll_extension",
                token="tok", account="credential 'Default' (bot)", root_module="payroll_zm",
                enterprise=True, modules=[], deps=[], enterprise_repo="https://github.com/odoo/enterprise.git",
                enterprise_branch="19.0", enterprise_token="ptok", enterprise_account="credential 'Partner'",
                db_name="d", mem_limit="1g", build_mem_limit="2g", cpus=1, timeout_s=60, health_timeout_s=60)
    base.update(kw)
    return pipeline.BuildSpec(**base)


def test_load_spec_enterprise_source_per_version(s, tmp_logs):
    build, _, _ = _payroll(s, edition="enterprise")
    partner = GithubCredential(name="Partner", username="odoo-partner", token_enc=encrypt("ptok"))
    v = s.get(OdooVersion, "18.0")
    v.enterprise_repo_url, v.enterprise_branch, v.enterprise_credential = (
        "https://github.com/odoo/enterprise.git", "saas-18.4", partner)
    s.commit()
    spec = pipeline.load_spec(build.id)
    assert (spec.enterprise_repo, spec.enterprise_branch, spec.enterprise_token, spec.enterprise_account) == (
        "https://github.com/odoo/enterprise.git", "saas-18.4", "ptok", "credential 'Partner' (odoo-partner)")


def test_enterprise_failures_are_clear(tmp_logs, monkeypatch):
    blog = logs.BuildLog(1)
    with pytest.raises(pipeline.BuildFailed) as e:
        pipeline._fetch_enterprise(_spec(enterprise_repo=""), blog)
    assert str(e.value) == "No Enterprise repository is set for Odoo 19.0 (Odoo versions page)"

    def missing(url, branch, token):
        raise pipeline.gitmirror.GitError("git fetch failed: fatal: couldn't find remote ref refs/heads/19.0")
    monkeypatch.setattr(pipeline.gitmirror, "enterprise_checkout", missing)
    with pytest.raises(pipeline.BuildFailed) as e:
        pipeline._fetch_enterprise(_spec(), blog)
    assert str(e.value) == ("Odoo Enterprise 19.0: odoo/enterprise has no 19.0 branch (credential 'Partner'). "
                            "Set the Enterprise source for 19.0 on the Odoo versions page.")

    def refused(url, branch, token):
        raise pipeline.gitmirror.GitError(
            "git fetch failed: remote: Repository not found.\nfatal: repository 'https://github.com/odoo/enterprise.git/' not found")
    monkeypatch.setattr(pipeline.gitmirror, "enterprise_checkout", refused)
    with pytest.raises(pipeline.BuildFailed) as e:
        pipeline._fetch_enterprise(_spec(), blog)
    msg = str(e.value)
    assert msg.startswith("Odoo Enterprise 19.0: GitHub refused access to odoo/enterprise with credential 'Partner'.")
    assert "Git credentials page" in msg and "ptok" not in msg


def test_project_fetch_access_error(tmp_logs, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "build_dir", lambda bid: tmp_path / "b")
    def refused(path, url, token):
        raise pipeline.gitmirror.GitError("git fetch failed: fatal: Authentication failed for 'https://github.com/a/x.git/'")
    monkeypatch.setattr(pipeline.gitmirror, "fetch_mirror", refused)
    with pytest.raises(pipeline.BuildFailed) as e:
        pipeline._prepare(_spec(enterprise=False), logs.BuildLog(1))
    assert "Project repository: GitHub refused access to a/payroll_extension with credential 'Default' (bot)" in str(e.value)
    assert "Fetching a/payroll_extension with credential 'Default' (bot)" in logs.read_log(1)


# ---------- combined previews ----------

def _combo(s):
    from previewer.models import COMBO_BRANCH
    s.add(GithubCredential(name="Default", username="bot", token_enc=encrypt("tok"), is_default=True))
    p = Project(kind="combo", name="HR suite", slug="hr-suite", repo_url="",
                default_odoo_version="18.0", edition="community")
    pay = ProjectDependency(project=p, repo_url="https://github.com/a/payroll_extension",
                            module_name="payroll_zm")
    bank = ProjectDependency(project=p, repo_url="https://github.com/a/bank_extension.git",
                             module_name="bank_zm", position=1)
    b = Branch(project=p, name=COMBO_BRANCH, slug="combined", host="hr-suite.preview.localhost",
               last_commit_sha="c" * 40)
    s.add_all([p, pay, bank, b])
    s.flush()
    b.dependency_branches = {str(pay.id): "v18-astro", str(bank.id): "main"}
    build = Build(branch=b, commit_sha="c" * 40, odoo_version="18.0", status="queued")
    s.add(build)
    s.commit()
    return build, pay, bank


def test_combo_spec_and_prepare_installs_all_parts(s, tmp_logs, tmp_path, monkeypatch):
    build, pay, bank = _combo(s)
    spec = pipeline.load_spec(build.id)
    assert spec.combo and [(d.name, d.branch) for d in spec.deps] == [
        ("payroll_zm", "v18-astro"), ("bank_zm", "main")]
    monkeypatch.setattr(pipeline, "build_dir", lambda bid: tmp_path / "b")
    monkeypatch.setattr(pipeline, "filestore_dir", lambda bid: tmp_path / "fs")
    monkeypatch.setattr(pipeline.gitmirror, "fetch_mirror",
                        lambda *a: pytest.fail("a combo has no project repo to fetch"))

    def fake_deps(spec_, bdir, blog):
        for i, d in enumerate(spec_.deps):
            _module(bdir / "extra" / str(i), d.name)
    monkeypatch.setattr(pipeline, "_fetch_dependencies", fake_deps)
    monkeypatch.setattr(pipeline.docker_ops, "ensure_image", lambda img, v, req, blog: img)
    mounts = pipeline._prepare(spec, logs.BuildLog(build.id))
    assert spec.modules == ["bank_zm", "payroll_zm"]
    targets = [m["Target"] for m in mounts]
    assert docker_ops.SRC not in targets and f"{docker_ops.EXTRA}/1" in targets
    conf = (tmp_path / "b" / "odoo.conf").read_text()
    assert docker_ops.SRC not in conf and f"{docker_ops.EXTRA}/0" in conf
    assert "Combined preview of 2 part(s): payroll_zm@v18-astro, bank_zm@main" in logs.read_log(build.id)


def test_combo_without_parts_fails(tmp_logs, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "build_dir", lambda bid: tmp_path / "b")
    with pytest.raises(pipeline.BuildFailed, match="no parts"):
        pipeline._prepare(_spec(combo=True, enterprise=False), logs.BuildLog(1))


def test_plain_odoo_installs_base_without_addons_path(s, tmp_logs, tmp_path, monkeypatch):
    build, _, _ = _payroll(s)
    monkeypatch.setattr(pipeline, "build_dir", lambda bid: tmp_path / "b")
    monkeypatch.setattr(pipeline.docker_ops, "ensure_image", lambda img, v, req, blog: img)
    monkeypatch.setattr(pipeline.docker_ops, "core_modules", lambda image: {"base", "web", "crm"})
    spec = _spec(build_id=build.id, combo=True, plain=True, enterprise=False)
    mounts = pipeline._prepare(spec, logs.BuildLog(build.id))
    assert spec.modules == ["base"]
    assert docker_ops.SRC not in [m["Target"] for m in mounts]
    assert "addons_path" not in (tmp_path / "b" / "odoo.conf").read_text()
    assert "no custom code" in logs.read_log(build.id)


def test_duplicate_modules_across_parts(tmp_path):
    _module(tmp_path / "x", "bank_zm")
    _module(tmp_path / "y", "bank_zm")
    _module(tmp_path / "y", "other")
    found = pipeline.module_sources([("a/bank_extension", tmp_path / "x"), ("a/bank_fork", tmp_path / "y")])
    assert found["other"] == ["a/bank_fork"]
    assert pipeline.duplicate_modules(found) == "Module bank_zm is provided by both a/bank_extension and a/bank_fork"
    assert pipeline.duplicate_modules({"a": ["x"]}) is None


def test_preview_config_listens_on_all_interfaces():
    conf = docker_ops.render_conf(db_name="d", admin_passwd="x", addons="/mnt/src")
    assert "http_interface = 0.0.0.0" in conf  # Odoo 20 defaults to 127.0.0.1
