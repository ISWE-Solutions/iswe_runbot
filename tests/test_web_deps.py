"""Project dependencies, branch selection (picker) and the pre-build dependency check."""
import pytest

from previewer import deps as builddeps
from previewer import github
from previewer.config import get_settings
from previewer.models import Branch, Build, Job, Project, ProjectDependency
from previewer.security import encrypt
from previewer.web import accounts as web_accounts
from previewer.web import selection
from previewer.web.routes import dependencies

from test_web_app import as_role, make_account, make_branch, make_project  # noqa: F401  (fixtures/helpers)

OK = builddeps.CheckResult("bank_zm", "https://github.com/acme/bank_extension", "main", True)
MISSING = builddeps.CheckResult("bank_zm", "https://github.com/acme/bank_extension", "19.0", False,
                                "bank_extension has no branch '19.0'")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Dependency checks pass unless a test says otherwise; caches start empty."""
    selection._blocked.clear()
    dependencies._branch_cache.clear()
    web_accounts.clear_enterprise_cache()
    monkeypatch.setattr(builddeps, "check", lambda branch, version=None: [OK])
    monkeypatch.setattr(github, "list_branches", lambda url, token: [
        github.BranchInfo(name=n, sha="f" * 40) for n in ("main", "v19", "zra-frontline-changes")])
    yield


def checks(monkeypatch, *results):
    monkeypatch.setattr(builddeps, "check", lambda branch, version=None: list(results))


def add_dep(s, p, **kw) -> ProjectDependency:
    d = ProjectDependency(project_id=p.id, repo_url="https://github.com/acme/bank_extension.git",
                          module_name="bank_zm", branch_map={"18.0": "main"}, **kw)
    s.add(d)
    s.commit()
    return d


def unselected(s, p, name, sha="c" * 40) -> Branch:
    b = make_branch(s, p, name=name, slug=name.replace("/", "-"), sha=sha)
    b.enabled = False
    s.commit()
    return b


HX = {"HX-Request": "true", "HX-Target": "branches-table"}


# ---------- picker ----------

def test_picker_selection_filter(s, as_role):
    p = make_project(s)
    make_branch(s, p, name="v18", slug="v18")
    unselected(s, p, "v18-beta")
    c = as_role("tester")
    default = c.get(f"/projects/{p.id}").text          # some selected -> "Selected" view
    assert ">v18<" in default and "v18-beta" not in default and "1 of 2 branches selected" in default
    assert "v18-beta" in c.get(f"/projects/{p.id}?sel=unselected").text
    assert ">v18<" not in c.get(f"/projects/{p.id}?sel=unselected").text
    every = c.get(f"/projects/{p.id}?sel=all&q=beta").text
    assert "v18-beta" in every and ">v18<" not in every


def test_picker_defaults_to_all_when_nothing_selected(s, as_role):
    p = make_project(s)
    unselected(s, p, "v18")
    r = as_role("tester").get(f"/projects/{p.id}")
    assert ">v18<" in r.text and "0 of 1 branches selected" in r.text


def test_select_ok_queues_build(s, as_role):
    p = make_project(s)
    b = unselected(s, p, "v18-beta")
    r = as_role("developer").post(f"/branches/{b.id}/select?sel=all", headers=HX)
    assert r.status_code == 200 and 'id="branches-table"' in r.text
    s.expire_all()
    assert s.get(Branch, b.id).enabled
    assert s.query(Build).one().status == "queued"


def test_select_blocked_then_build_anyway(s, as_role, monkeypatch):
    p = make_project(s)
    add_dep(s, p)
    b = unselected(s, p, "v19")
    checks(monkeypatch, MISSING)
    c = as_role("developer")
    r = c.post(f"/branches/{b.id}/select?sel=all", headers=HX)
    assert "Blocked:" in r.text and "has no branch" in r.text and "Build anyway" in r.text
    s.expire_all()
    assert s.get(Branch, b.id).enabled and s.query(Build).count() == 0
    assert "Blocked:" in c.get(f"/branches/{b.id}").text

    c.post(f"/branches/{b.id}/build-anyway", headers=HX)
    s.expire_all()
    assert s.query(Build).one().status == "queued"
    assert "Blocked:" not in c.get(f"/projects/{p.id}?sel=all").text


def test_check_again_queues_once_fixed(s, as_role, monkeypatch):
    p = make_project(s)
    b = unselected(s, p, "v19")
    checks(monkeypatch, MISSING)
    c = as_role("developer")
    c.post(f"/branches/{b.id}/select", headers=HX)
    checks(monkeypatch, OK)
    c.post(f"/branches/{b.id}/select", headers=HX)      # "Check again"
    s.expire_all()
    assert s.query(Build).count() == 1


def test_check_endpoint_lists_results(s, as_role, monkeypatch):
    p = make_project(s)
    b = make_branch(s, p)
    checks(monkeypatch, OK, MISSING)
    r = as_role("tester").post(f"/branches/{b.id}/check")
    assert r.status_code == 200 and "✓ bank_zm @ main" in r.text and "✗ bank_zm @ 19.0" in r.text
    s.expire_all()
    assert s.query(Build).count() == 0


def test_bulk_select_and_unselect(s, as_role, monkeypatch):
    p = make_project(s)
    a = unselected(s, p, "v18-a", sha="1" * 40)
    b = unselected(s, p, "v18-b", sha="2" * 40)
    c = as_role("developer")
    r = c.post(f"/projects/{p.id}/branches/bulk?sel=all", data={"ids": [a.id, b.id], "action": "select"},
               headers=HX)
    assert r.status_code == 200 and "Building v18-a, v18-b" in r.text
    s.expire_all()
    assert s.query(Build).count() == 2 and all(x.enabled for x in s.query(Branch).all())

    s.add(Build(branch_id=a.id, commit_sha="1" * 40, odoo_version="18.0", status="running",
                container_id="x"))
    s.commit()
    c.post(f"/projects/{p.id}/branches/bulk", data={"ids": [a.id, b.id], "action": "unselect"}, headers=HX)
    s.expire_all()
    assert not any(x.enabled for x in s.query(Branch).all())
    assert s.query(Job).filter_by(type="stop").count() == 1
    assert s.query(Job).filter_by(type="build", status="cancelled").count() == 2


def test_bulk_select_reports_blocked(s, as_role, monkeypatch):
    p = make_project(s)
    a = unselected(s, p, "v19")
    checks(monkeypatch, MISSING)
    r = as_role("developer").post(f"/projects/{p.id}/branches/bulk?sel=all",
                                  data={"ids": [a.id], "action": "select"}, headers=HX)
    assert "blocked by missing dependencies: v19" in r.text
    s.expire_all()
    assert s.get(Branch, a.id).enabled and s.query(Build).count() == 0


def test_version_change_runs_check(s, as_role, monkeypatch):
    p = make_project(s)
    b = make_branch(s, p)
    checks(monkeypatch, MISSING)
    r = as_role("developer").post(f"/branches/{b.id}/version", data={"odoo_version": "19.0"}, headers=HX)
    assert "Blocked:" in r.text
    s.expire_all()
    assert s.get(Branch, b.id).odoo_version_override == "19.0" and s.query(Build).count() == 0


def test_rebuild_requires_selection(s, as_role):
    p = make_project(s)
    b = unselected(s, p, "v18-beta")
    as_role("developer").post(f"/branches/{b.id}/rebuild")
    assert s.query(Build).count() == 0


# ---------- dependencies ----------

def dep_form(**kw):
    return {"repo_url": "https://github.com/acme/bank_extension.git", "module_name": "bank_zm",
            "credential_id": "", "branch_18.0": "main", "branch_19.0": "v19", **kw}


def test_dependency_crud_and_account_choice(s, as_role):
    p = make_project(s)
    partner = make_account(s, name="Partner", token="ghp_deptoken42", default=False)
    c = as_role("admin")
    r = c.post(f"/projects/{p.id}/deps/new", data=dep_form(credential_id=str(partner.id)))
    assert r.status_code == 303 and r.headers["location"] == f"/projects/{p.id}?tab=deps"
    d = s.query(ProjectDependency).one()
    assert d.branch_map == {"18.0": "main", "19.0": "v19"} and d.root_module_name == "bank_zm"
    assert d.credential_id == partner.id

    tab = c.get(f"/projects/{p.id}?tab=deps").text
    assert "bank_zm" in tab and "Partner" in tab and "ghp_deptoken42" not in tab
    edit = c.get(f"/projects/{p.id}/deps/{d.id}/edit").text
    assert "ghp_deptoken42" not in edit and f'value="{partner.id}" selected' in edit
    assert "Same as project (Default)" in edit

    c.post(f"/projects/{p.id}/deps/{d.id}/edit", data=dep_form(**{"branch_19.0": "", "credential_id": str(partner.id)}))
    s.expire_all()
    d = s.get(ProjectDependency, d.id)
    assert d.branch_map == {"18.0": "main"} and d.credential_id == partner.id

    c.post(f"/projects/{p.id}/deps/{d.id}/edit", data=dep_form())
    s.expire_all()
    assert s.get(ProjectDependency, d.id).credential_id is None
    assert "same as project (Default)" in c.get(f"/projects/{p.id}?tab=deps").text

    c.post(f"/projects/{p.id}/deps/{d.id}/delete")
    assert s.query(ProjectDependency).count() == 0


def test_dependency_validation(s, as_role):
    p = make_project(s)
    add_dep(s, p)
    c = as_role("admin")
    dup = c.post(f"/projects/{p.id}/deps/new", data=dep_form(repo_url="https://github.com/acme/bank_extension"))
    assert dup.status_code == 400 and "already a dependency" in dup.text
    own = c.post(f"/projects/{p.id}/deps/new", data=dep_form(repo_url=p.repo_url))
    assert own.status_code == 400 and "own repository" in own.text
    bad = c.post(f"/projects/{p.id}/deps/new", data=dep_form(repo_url="https://github.com/acme/x", module_name="Bad-1"))
    assert bad.status_code == 400 and "Lowercase" in bad.text


def test_dependency_move(s, as_role):
    p = make_project(s)
    a = add_dep(s, p, position=0)
    b = ProjectDependency(project_id=p.id, repo_url="https://github.com/acme/utils", position=1)
    s.add(b)
    s.commit()
    as_role("admin").post(f"/projects/{p.id}/deps/{b.id}/move", data={"dir": "up"})
    s.expire_all()
    assert [d.id for d in s.get(Project, p.id).dependencies] == [b.id, a.id]


def test_load_branches_for_dependency_form(s, as_role):
    p = make_project(s)
    r = as_role("admin").post(f"/projects/{p.id}/deps/branches",
                              data={"repo_url": "https://github.com/acme/bank_extension", "branch_19.0": "v19"})
    assert r.status_code == 200 and "3 branches loaded" in r.text
    assert '<option value="v19" selected>v19</option>' in r.text
    assert "zra-frontline-changes" in r.text


def test_enterprise_row_availability(s, as_role, monkeypatch):
    p = make_project(s, edition="enterprise")
    monkeypatch.setattr(get_settings(), "enterprise_repo_url", "https://github.com/acme/enterprise.git")
    monkeypatch.setattr(github, "branch_exists", lambda url, tok, br: br == "18.0")
    tab = as_role("developer").get(f"/projects/{p.id}?tab=deps").text
    assert "Odoo Enterprise" in tab and "✓ 18.0" in tab and "has no 19.0 branch" in tab


def test_dependency_permissions(s, as_role):
    p = make_project(s)
    d = add_dep(s, p)
    b = unselected(s, p, "v18-beta")
    tester = as_role("tester")
    assert tester.post(f"/branches/{b.id}/select").status_code == 403
    assert tester.get(f"/projects/{p.id}/deps/new").status_code == 403
    assert "bank_zm" in tester.get(f"/projects/{p.id}?tab=deps").text      # can view
    dev = as_role("developer")
    assert dev.get(f"/projects/{p.id}/deps/{d.id}/edit").status_code == 403
    assert dev.post(f"/projects/{p.id}/deps/new", data=dep_form(repo_url="https://github.com/acme/y")).status_code == 403
    assert "Add dependency" not in dev.get(f"/projects/{p.id}?tab=deps").text
    assert dev.post(f"/branches/{b.id}/select").status_code == 303
    assert as_role("admin").get(f"/projects/{p.id}/deps/new").status_code == 200


# ---------- branch overrides ----------

def test_branch_dependency_override_saved_and_shown(s, as_role):
    p = make_project(s)
    d = add_dep(s, p)
    b = make_branch(s, p, name="v18-astro", slug="v18-astro")
    c = as_role("developer")
    page = c.get(f"/branches/{b.id}/settings").text
    assert "Project default (main)" in page and "Builds now use <code>main</code> (project mapping)" in page
    r = c.post(f"/branches/{b.id}/settings", data={"enabled": "on", f"dep_{d.id}": "zra-frontline-changes",
                                                   "demo_data_override": "default"})
    assert r.status_code == 303
    s.expire_all()
    b = s.get(Branch, b.id)
    assert b.dependency_branches == {str(d.id): "zra-frontline-changes"}
    assert s.query(Build).one().status == "queued"                      # resolved deps changed
    assert "+ bank_zm @ zra-frontline-changes" in c.get(f"/projects/{p.id}").text
    assert "(branch override)" in c.get(f"/branches/{b.id}/settings").text

    c.post(f"/branches/{b.id}/settings", data={"enabled": "on", f"dep_{d.id}": "", "demo_data_override": "default"})
    s.expire_all()
    assert s.get(Branch, b.id).dependency_branches == {}


def test_branch_settings_blocked_by_check(s, as_role, monkeypatch):
    p = make_project(s)
    d = add_dep(s, p)
    b = make_branch(s, p)
    checks(monkeypatch, MISSING)
    c = as_role("developer")
    r = c.post(f"/branches/{b.id}/settings", data={"enabled": "on", f"dep_{d.id}": "nope",
                                                   "demo_data_override": "default"})
    assert r.headers["location"] == f"/branches/{b.id}/settings"
    assert s.query(Build).count() == 0
    assert "Not built: the dependency check failed" in c.get(f"/branches/{b.id}/settings").text


def test_override_dropdown_loads_branches(s, as_role):
    p = make_project(s)
    d = add_dep(s, p)
    b = make_branch(s, p)
    r = as_role("developer").post(f"/branches/{b.id}/dep-branches/{d.id}")
    assert r.status_code == 200 and "zra-frontline-changes" in r.text and "Project default (main)" in r.text


def test_deleting_dependency_drops_branch_overrides(s, as_role):
    p = make_project(s)
    d = add_dep(s, p)
    b = make_branch(s, p)
    b.dependency_branches = {str(d.id): "x"}
    s.commit()
    as_role("admin").post(f"/projects/{p.id}/deps/{d.id}/delete")
    s.expire_all()
    assert s.get(Branch, b.id).dependency_branches == {}


# ---------- board ----------

def test_board_hides_unselected_and_links_picker(s, as_role):
    p = make_project(s)
    make_branch(s, p, name="v18", slug="v18")
    unselected(s, p, "v18-beta")
    unselected(s, p, "v18-zta", sha="d" * 40)
    r = as_role("tester").get("/?project=all")
    assert ">v18<" in r.text and "v18-beta" not in r.text
    assert "2 branches not selected" in r.text and f"/projects/{p.id}?tab=branches&sel=unselected" in r.text


def test_board_card_shows_dependency_refs(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    s.add(Build(branch_id=b.id, commit_sha="a" * 40, odoo_version="18.0", status="running",
                dependency_refs=[{"name": "bank_zm", "repo_url": "x", "branch": "main", "sha": "b" * 40,
                                  "source": "project"}]))
    s.commit()
    assert "+ bank_zm@main" in as_role("tester").get("/").text


# ---------- project create / settings tab ----------

def test_settings_tab_admin_only_and_errors_render_in_tab(s, as_role):
    p = make_project(s)
    dev = as_role("developer")
    assert "Auto-select new branches" not in dev.get(f"/projects/{p.id}?tab=settings").text
    adm = as_role("admin")
    r = adm.post(f"/projects/{p.id}/edit", data={"name": "Payroll", "repo_url": p.repo_url,
                                                 "default_odoo_version": "18.0", "edition": "community",
                                                 "auto_build_regex": "(["})
    assert r.status_code == 400 and "Invalid regex" in r.text and 'aria-current="page"' in r.text
