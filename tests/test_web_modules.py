import pytest

from previewer import deps
from previewer.models import Branch, Build, ModuleSet, Project
from previewer.web.views import error_headline
from test_web_app import as_role, make_branch, make_project  # noqa: F401  (fixture)
from test_web_combos import checks_ok, combo_form, make_combo  # noqa: F401  (fixture)

REPO = {
    "acme_base": ["base", "hr"],
    "acme_payroll": ["acme_base"],
    "acme_reports": ["acme_payroll"],
    "acme_portal": ["acme_base", "portal"],
    "acme_extra": ["ghost_module"],
}
CORE = {"base", "hr", "portal"}


@pytest.fixture
def github_modules(monkeypatch):
    """Manifests as if read from GitHub: the project repo has REPO, other repos one module."""
    def manifests(repo_url, token, ref, root):
        return dict(REPO) if "payroll" in repo_url else {root: ["base"]}
    monkeypatch.setattr(deps, "repo_manifests", manifests)
    monkeypatch.setattr(deps, "core_modules", lambda v, t="": set(CORE))
    monkeypatch.setattr(deps, "enterprise_modules", lambda s, v: {"hr_payroll"})


def _setup(s):
    p = make_project(s)
    b = make_branch(s, p)
    b.enabled = True
    s.commit()
    return p, b


def test_modules_tab_lists_available_and_top_level(s, as_role, github_modules):
    p, b = _setup(s)
    c = as_role("tester")
    assert c.get(f"/projects/{p.id}?tab=modules").status_code == 200
    r = c.get(f"/projects/{p.id}/modules/available?branch_id={b.id}")
    assert r.status_code == 200
    assert "Install these <strong>3</strong>" in r.text and "all <strong>5</strong>" in r.text
    assert "top-level" in r.text and "ghost_module" in r.text
    assert "Create set from top-level modules" not in r.text  # testers cannot create sets
    assert "Create set from top-level modules" in as_role("developer").get(
        f"/projects/{p.id}/modules/available?branch_id={b.id}").text


def test_create_set_from_top_level(s, as_role, github_modules):
    p, b = _setup(s)
    r = as_role("developer").post(f"/projects/{p.id}/module-sets/top-level", data={"branch_id": b.id})
    assert r.status_code == 303
    ms = s.query(ModuleSet).one()
    assert ms.modules == "acme_extra,acme_portal,acme_reports" and "5 modules" in ms.description


def test_module_set_crud_default_and_effective_modules(s, as_role, github_modules):
    p, b = _setup(s)
    c = as_role("developer")
    assert c.get(f"/projects/{p.id}/module-sets/new").status_code == 200
    r = c.post(f"/projects/{p.id}/module-sets/new", data={
        "name": "Reports", "modules": ["acme_reports"], "modules_text": "acme_portal, acme_reports",
        "make_default": "on", "branch_id": b.id})
    assert r.status_code == 303
    s.expire_all()
    ms = s.query(ModuleSet).one()
    assert ms.modules == "acme_reports,acme_portal"
    assert s.get(Project, p.id).module_set_id == ms.id
    assert s.get(Branch, b.id).effective_modules == "acme_reports,acme_portal"

    # duplicate name and bad module names are refused
    dup = c.post(f"/projects/{p.id}/module-sets/new", data={"name": "reports", "modules_text": "acme_base"})
    assert dup.status_code == 400 and "has that name" in dup.text
    bad = c.post(f"/projects/{p.id}/module-sets/new", data={"name": "Bad", "modules_text": "Not-A-Module"})
    assert bad.status_code == 400 and "Invalid module name" in bad.text

    r = c.post(f"/projects/{p.id}/module-sets/{ms.id}/edit",
               data={"name": "Reports only", "modules_text": "acme_reports"}, follow_redirects=True)
    assert "rebuild those to apply" in r.text
    s.expire_all()
    assert s.get(Branch, b.id).effective_modules == "acme_reports"

    c.post(f"/projects/{p.id}/module-sets/{ms.id}/delete")
    s.expire_all()
    assert s.query(ModuleSet).count() == 0 and s.get(Project, p.id).module_set_id is None
    assert s.get(Branch, b.id).effective_modules == ""


def test_plan_preview_explains_missing_dependency(s, as_role, github_modules):
    p, b = _setup(s)
    c = as_role("tester")
    r = c.post(f"/projects/{p.id}/modules/plan", data={"branch_id": b.id, "modules_text": "acme_extra, acme_reports"})
    assert r.status_code == 200
    assert "acme_extra cannot install: it depends on ghost_module" in r.text
    assert "acme_payroll" in r.text and "pulled in as a dependency" in r.text
    ok = c.post(f"/projects/{p.id}/modules/plan", data={"branch_id": b.id, "modules": ["acme_reports"]})
    assert "cannot install" not in ok.text and "3 modules (1 asked for, 2 pulled in" in " ".join(ok.text.split())


def test_branch_settings_module_choice(s, as_role, github_modules, checks_ok):
    p, b = _setup(s)
    ms = ModuleSet(project_id=p.id, name="Top", modules="acme_reports,acme_portal")
    s.add(ms)
    s.commit()
    c = as_role("developer")
    base = {"odoo_version_override": "", "demo_data_override": "default", "enabled": "on", "auto_rebuild": "on"}
    r = c.post(f"/branches/{b.id}/settings", data={**base, "modules_mode": "set", "module_set_id": ms.id})
    assert r.status_code == 303
    s.expire_all()
    b = s.get(Branch, b.id)
    assert b.module_set_id == ms.id and b.modules_override is None
    assert b.effective_modules == "acme_reports,acme_portal"
    assert s.query(Build).filter_by(status="queued").count() == 1

    c.post(f"/branches/{b.id}/settings", data={**base, "modules_mode": "custom", "modules_override": "acme_base"})
    s.expire_all()
    b = s.get(Branch, b.id)
    assert b.module_set_id is None and b.modules_override == "acme_base"

    c.post(f"/branches/{b.id}/settings", data={**base, "modules_mode": "default", "modules_override": "ignored"})
    s.expire_all()
    b = s.get(Branch, b.id)
    assert b.module_set_id is None and b.modules_override is None

    empty = c.post(f"/branches/{b.id}/settings", data={**base, "modules_mode": "custom", "modules_override": ""})
    assert empty.status_code == 400


def test_branch_plan_and_page(s, as_role, github_modules):
    p, b = _setup(s)
    c = as_role("tester")
    assert "Install plan" in c.get(f"/branches/{b.id}").text
    r = c.post(f"/branches/{b.id}/plan")
    assert r.status_code == 200 and "acme_extra cannot install" in r.text  # auto = every module


def test_build_page_shows_install_plan(s, as_role):
    p, b = _setup(s)
    plan = {"requested": ["acme_reports", "acme_extra"], "install": ["acme_reports", "acme_payroll", "acme_base"],
            "core": ["base", "hr"], "missing": {"acme_extra": ["ghost_module"]},
            "sources": {"acme_reports": "Payroll repo"}, "top_level": ["acme_reports"]}
    build = Build(branch_id=b.id, commit_sha="a" * 40, odoo_version="18.0", status="failed", install_plan=plan,
                  error_summary="acme_extra cannot install: it depends on ghost_module (not found in the repo)\n"
                                "2026 ERROR odoo: something else")
    s.add(build)
    s.commit()
    r = as_role("tester").get(f"/builds/{build.id}")
    assert r.status_code == 200
    assert "Modules could not install" in r.text and "Install plan" in r.text
    assert "ghost_module" in r.text and "acme_payroll" in r.text


def test_error_headline_keeps_dependency_lines():
    summary = ("a_mod cannot install: it depends on x\nb_mod cannot install: it depends on y\n"
               "Traceback (most recent call last):")
    assert error_headline(summary) == "a_mod cannot install: it depends on x\nb_mod cannot install: it depends on y"
    assert error_headline("Odoo crashed\nmore") == "Odoo crashed"


def test_module_sets_permissions(s, as_role, github_modules):
    p, b = _setup(s)
    c = as_role("tester")
    assert c.get(f"/projects/{p.id}/module-sets/new").status_code == 403
    assert c.post(f"/projects/{p.id}/module-sets/new", data={"name": "X", "modules_text": "acme_base"}).status_code == 403
    assert c.post(f"/projects/{p.id}/module-sets/default", data={"module_set_id": ""}).status_code == 403
    assert s.query(ModuleSet).count() == 0


def test_combo_modules_tab_and_settings_set(s, as_role, github_modules, checks_ok):
    make_project(s)
    c = as_role("developer")
    combo = make_combo(s, c)
    assert c.get(f"/combos/{combo.id}?tab=modules").status_code == 200
    r = c.get(f"/projects/{combo.id}/modules/available")
    assert "Install these" in r.text  # all parts' modules
    ms = ModuleSet(project_id=combo.id, name="Payroll top", modules="acme_reports")
    s.add(ms)
    s.commit()
    form = combo_form(slug="hr-suite", module_set_id=str(ms.id))
    r = c.post(f"/combos/{combo.id}/settings", data=form)
    assert r.status_code == 303
    s.expire_all()
    combo = s.get(Project, combo.id)
    assert combo.module_set_id == ms.id
    assert combo.branches[0].effective_modules == "acme_reports"
