"""Combined previews: several repos, each at a chosen branch, in one Odoo."""
import pytest

from previewer import deps as builddeps
from previewer import github
from previewer.models import COMBO_BRANCH, Branch, Build, Job, Project, ProjectDependency
from previewer.web import selection
from test_web_app import make_branch, make_project, user_client  # noqa: F401  (fixture)


@pytest.fixture
def checks_ok(monkeypatch):
    monkeypatch.setattr(selection, "run_check", lambda b, v=None: [
        builddeps.CheckResult("bank_zm", "", "main", True)])


@pytest.fixture
def checks_blocked(monkeypatch):
    monkeypatch.setattr(selection, "run_check", lambda b, v=None: [
        builddeps.CheckResult("bank_zm", "", "zra-x", False, "bank_extension has no branch 'zra-x'")])


def combo_form(**kw):
    form = {
        "name": "HR suite", "slug": "", "default_odoo_version": "18.0", "edition": "enterprise",
        "modules_to_install": "",
        "parts-0-source": "other", "parts-0-repo_url": "https://github.com/acme/payroll_extension.git",
        "parts-0-module_name": "payroll_zm", "parts-0-credential_id": "", "parts-0-branch": "v18-astro",
        "parts-0-dep_id": "",
        "parts-1-source": "other", "parts-1-repo_url": "https://github.com/acme/bank_extension.git",
        "parts-1-module_name": "bank_zm", "parts-1-credential_id": "", "parts-1-branch": "main",
        "parts-1-dep_id": "",
    }
    form.update(kw)
    return form


def make_combo(s, c, **kw) -> Project:
    """c: a signed-in client."""
    r = c.post("/combos/new", data=combo_form(**kw))
    assert r.status_code == 303, r.text
    s.expire_all()
    return s.query(Project).filter_by(kind="combo").one()


def test_create_combo_pins_parts_and_refreshes(s, user_client, checks_ok):
    make_project(s)  # default account
    p = make_combo(s, user_client)
    assert p.slug == "hr-suite" and p.repo_url == "" and p.edition == "enterprise"
    b = s.query(Branch).filter_by(project_id=p.id).one()
    assert b.name == COMBO_BRANCH and b.enabled and b.host == "hr-suite.preview.localhost"
    names = {d.root_module_name: d for d in p.dependencies}
    assert set(names) == {"payroll_zm", "bank_zm"}
    assert b.dependency_branches == {str(names["payroll_zm"].id): "v18-astro",
                                     str(names["bank_zm"].id): "main"}
    job = s.query(Job).filter_by(type="sync").one()
    assert job.payload == {"project_id": p.id}
    assert s.query(Build).count() == 0  # the worker's sync resolves the heads, then builds


def test_create_combo_blocked_does_not_refresh(s, user_client, checks_blocked):
    make_project(s)
    c = user_client
    r = c.post("/combos/new", data=combo_form(), follow_redirects=True)
    assert "not built" in r.text and "no branch" in r.text
    assert s.query(Job).filter_by(type="sync").count() == 0
    p = s.query(Project).filter_by(kind="combo").one()
    b = p.branches[0]
    assert "Blocked:" in c.get(f"/combos/{p.id}").text
    # Build anyway refreshes the parts (no combined commit known yet)
    c.post(f"/branches/{b.id}/build-anyway")
    assert s.query(Job).filter_by(type="sync").count() == 1


def test_create_combo_validation(s, user_client, checks_ok):
    make_project(s)
    c = user_client
    r = c.post("/combos/new", data=combo_form(**{"parts-0-branch": "", "parts-1-repo_url": "nope"}))
    assert r.status_code == 400 and "choose a branch" in r.text and "Not a GitHub repository" in r.text
    r = c.post("/combos/new", data={k: v for k, v in combo_form().items() if not k.startswith("parts-")})
    assert r.status_code == 400 and "Add at least one repository" in r.text
    dup = combo_form(**{"parts-1-repo_url": "https://github.com/acme/payroll_extension"})
    assert "already a part" in c.post("/combos/new", data=dup).text
    make_combo(s, c)
    assert "uses this slug" in c.post("/combos/new", data=combo_form()).text
    assert s.query(Project).filter_by(kind="combo").count() == 1


def test_registered_project_part_prefills(s, user_client, checks_ok):
    reg = make_project(s, module_name="payroll_zm")
    c = user_client
    r = c.post("/combos/part-row", data={"row": "3", "action": "source", "parts-3-source": f"project:{reg.id}"})
    assert r.status_code == 200
    assert 'value="https://github.com/acme/payroll_extension"' in r.text and 'value="payroll_zm"' in r.text
    assert "readonly" in r.text
    form = combo_form(**{"parts-0-source": f"project:{reg.id}", "parts-0-repo_url": "",
                         "parts-0-module_name": ""})
    assert c.post("/combos/new", data=form).status_code == 303
    p = s.query(Project).filter_by(kind="combo").one()
    assert {d.repo_url for d in p.dependencies} >= {reg.repo_url}


def test_part_row_loads_branches(s, user_client, monkeypatch):
    make_project(s)
    monkeypatch.setattr(github, "list_branches", lambda url, tok: [github.BranchInfo("main", "1" * 40),
                                                                    github.BranchInfo("v19", "2" * 40)])
    r = user_client.post("/combos/part-row", data={
        "row": "0", "action": "load", "parts-0-source": "other",
        "parts-0-repo_url": "https://github.com/acme/bank_extension2"})
    assert "2 branches loaded" in r.text and '<option value="v19">' in r.text


def test_edit_parts_change_branch_add_remove(s, user_client, checks_ok):
    make_project(s)
    c = user_client
    p = make_combo(s, c)
    b = p.branches[0]
    pay, bank = sorted(p.dependencies, key=lambda d: d.position)
    bank_id = bank.id
    s.query(Job).delete()
    s.commit()
    page = c.get(f"/combos/{p.id}?tab=parts").text
    assert 'value="v18-astro"' in page
    r = c.post(f"/combos/{p.id}/parts", data={
        "parts-0-source": "other", "parts-0-repo_url": pay.repo_url, "parts-0-module_name": "payroll_zm",
        "parts-0-credential_id": "", "parts-0-branch": "v18-beta", "parts-0-dep_id": str(pay.id),
        "parts-5-source": "other", "parts-5-repo_url": "https://github.com/acme/hr_extras",
        "parts-5-module_name": "", "parts-5-credential_id": "", "parts-5-branch": "18.0", "parts-5-dep_id": "",
    })
    assert r.status_code == 303
    s.expire_all()
    deps = {d.root_module_name: d for d in s.get(Project, p.id).dependencies}
    assert set(deps) == {"payroll_zm", "hr_extras"} and bank_id not in {d.id for d in deps.values()}
    assert s.get(Branch, b.id).dependency_branches == {str(pay.id): "v18-beta", str(deps["hr_extras"].id): "18.0"}
    assert s.query(Job).filter_by(type="sync").count() == 1


def test_combo_preview_tab_and_settings(s, user_client, checks_ok):
    make_project(s)
    c = user_client
    p = make_combo(s, c)
    b = p.branches[0]
    b.last_commit_sha = "c" * 40
    s.add(Build(branch_id=b.id, commit_sha="c" * 40, odoo_version="18.0", status="running",
                host=b.host, container_id="x", db_name="hr_suite_combined_ccccccc_b1",
                dependency_refs=[{"name": "payroll_zm", "repo_url": "", "branch": "v18-astro",
                                  "sha": "abcdef1234", "source": "branch"}]))
    s.commit()
    page = c.get(f"/combos/{p.id}").text
    assert "Open hr-suite.preview.localhost" in page and "abcdef1" in page and "/18.0/hr-suite" in page
    assert "?tab=settings" in page
    form = {"name": "HR suite", "slug": "hr-suite", "default_odoo_version": "19.0", "edition": "enterprise",
            "modules_to_install": ""}
    assert c.post(f"/combos/{p.id}/settings", data=form).status_code == 303
    s.expire_all()
    assert s.get(Project, p.id).default_odoo_version == "19.0"
    assert s.query(Build).filter_by(status="queued").count() == 1  # version change rebuilds


def test_delete_combo(s, user_client, checks_ok):
    make_project(s)
    c = user_client
    p = make_combo(s, c)
    assert c.post(f"/combos/{p.id}/delete", data={"confirm_slug": "wrong"}).status_code == 303
    assert s.query(Project).filter_by(kind="combo").count() == 1
    assert c.post(f"/combos/{p.id}/delete", data={"confirm_slug": "hr-suite"}).headers["location"] == "/projects"
    s.expire_all()
    assert s.query(Project).filter_by(kind="combo").count() == 0
    assert s.query(ProjectDependency).count() == 0


def test_board_combined_tab_and_friendly_link(s, user_client, checks_ok):
    reg = make_project(s)
    make_branch(s, reg)
    p = make_combo(s, user_client)
    b = p.branches[0]
    s.add(Build(branch_id=b.id, commit_sha="c" * 40, odoo_version="18.0", status="running", host=b.host))
    s.commit()
    c = user_client
    board = c.get("/?project=combined").text
    assert "HR suite" in board and "combined" in board and "payroll_zm@v18-astro" in board
    assert ">v18<" not in board  # repo branches are not on the Combined tab
    assert "Combined &amp; plain</a>" in c.get("/?project=all").text
    r = c.get("/18.0/hr-suite")
    assert r.status_code == 302 and r.headers["location"] == "http://hr-suite.preview.localhost"
    assert c.get("/19.0/hr-suite").status_code == 404  # version mismatch page
    assert c.get(f"/projects/{p.id}").headers["location"] == f"/combos/{p.id}"
    assert c.get(f"/branches/{b.id}").headers["location"] == f"/combos/{p.id}"
    listing = c.get("/projects?tab=combined").text
    assert "Combined previews" in listing and f"/combos/{p.id}" in listing


def test_repo_projects_unaffected(s, user_client, checks_ok):
    reg = make_project(s)
    make_combo(s, user_client)
    page = user_client.get(f"/projects/{reg.id}").text
    assert "Sync branches now" in page and "Dependencies" in page
