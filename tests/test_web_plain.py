"""Plain Odoo previews ("Try Odoo"): a version, edition and demo choice, no custom code."""
import pytest

from previewer import deps as builddeps
from previewer.models import COMBO_BRANCH, Build, Job, Project
from previewer.web import accounts, selection
from test_web_app import make_project, user_client  # noqa: F401  (fixture)


@pytest.fixture
def ee(monkeypatch):
    """Enterprise readable for 18.0 and 19.0; checks pass."""
    def status(s, version, fresh=False):
        ok = version in ("18.0", "19.0")
        return {"repo_url": "https://github.com/ISWE-Solutions/enterprise_addons.git", "branch": version,
                "account": "credential 'ISWE dev'", "ok": ok,
                "message": "" if ok else f"enterprise_addons has no {version} branch"}
    monkeypatch.setattr(accounts, "enterprise_status", status)
    monkeypatch.setattr(selection, "run_check", lambda b, v=None: [])


def try_form(**kw):
    form = {"default_odoo_version": "19.0", "edition": "enterprise", "install_demo_data": "with",
            "apps": ["crm", "helpdesk"], "other_modules": "", "name": "", "slug": ""}
    form.update(kw)
    return form


def test_try_page_lists_versions_and_apps(s, user_client, ee):
    page = user_client.get("/try").text
    assert "18.0" in page and "19.0" in page and "Helpdesk" in page and 'class="flex items-center gap-2 rounded-md border' in page
    assert "enterprise_addons" in page  # availability passed to the page


def test_create_plain_preview(s, user_client, ee):
    r = user_client.post("/try", data=try_form())
    assert r.status_code == 303
    p = s.query(Project).filter_by(kind="plain").one()
    assert r.headers["location"] == f"/combos/{p.id}"
    assert (p.name, p.slug, p.default_odoo_version, p.edition) == ("Odoo 19 EE + demo", "odoo-19-ee-demo", "19.0", "enterprise")
    assert p.install_demo_data and p.modules_to_install == "crm,helpdesk" and p.repo_url == ""
    b = p.branches[0]
    assert b.name == COMBO_BRANCH and b.enabled and b.host == "odoo-19-ee-demo.preview.localhost"
    assert b.dependency_branches == {} and not p.dependencies
    job = s.query(Job).one()
    assert (job.type, job.payload) == ("sync", {"project_id": p.id})  # the worker queues the build
    assert s.query(Build).count() == 0


def test_second_preview_gets_free_slug(s, user_client, ee):
    dev = user_client
    dev.post("/try", data=try_form())
    dev.post("/try", data=try_form())
    assert sorted(p.slug for p in s.query(Project).all()) == ["odoo-19-ee-demo", "odoo-19-ee-demo-2"]


def test_enterprise_apps_need_enterprise(s, user_client, ee):
    dev = user_client
    r = dev.post("/try", data=try_form(edition="community"))
    assert r.status_code == 400 and "Helpdesk is an Enterprise app" in r.text
    r = dev.post("/try", data=try_form(edition="community", apps=["crm"],
                                                        install_demo_data="without", other_modules="l10n_zm"))
    assert r.status_code == 303
    p = s.query(Project).one()
    assert (p.name, p.edition, p.install_demo_data, p.modules_to_install) == ("Odoo 19 CE", "community", False, "crm,l10n_zm")


def test_enterprise_unavailable_for_version(s, user_client, ee, monkeypatch):
    from previewer.cli import seed_versions  # 20.0 is seeded enabled
    seed_versions()
    r = user_client.post("/try", data=try_form(default_odoo_version="20.0"))
    assert r.status_code == 400 and "Enterprise is not available for Odoo 20.0" in r.text
    assert s.query(Project).count() == 0


def test_plain_detail_tabs_and_settings_rebuild(s, user_client, ee):
    dev = user_client
    dev.post("/try", data=try_form(apps=["crm"]))
    p = s.query(Project).one()
    b = p.branches[0]
    b.last_commit_sha = "d" * 40  # what the worker's sync sets
    s.add(Build(branch_id=b.id, commit_sha="d" * 40, odoo_version="19.0", status="running",
                host=b.host, container_id="x", db_name="odoo_19_ee_demo_combined_ddddddd_b1"))
    s.commit()
    page = dev.get(f"/combos/{p.id}").text
    assert "Plain Odoo · no custom code" in page and "Odoo 19.0 · Enterprise · demo · CRM" in page
    assert f"/combos/{p.id}?tab=parts" not in page and "Refresh parts" not in page
    assert "Open odoo-19-ee-demo.preview.localhost" in page
    settings = dev.get(f"/combos/{p.id}?tab=settings").text
    assert 'name="apps" value="crm" checked' in settings
    form = {"name": p.name, "slug": p.slug, "default_odoo_version": "18.0", "edition": "community",
            "install_demo_data": "without", "apps": ["crm", "stock"], "other_modules": ""}
    assert dev.post(f"/combos/{p.id}/settings", data=form).status_code == 303
    s.expire_all()
    p = s.get(Project, p.id)
    assert (p.default_odoo_version, p.edition, p.install_demo_data, p.modules_to_install) == ("18.0", "community", False, "crm,stock")
    assert s.query(Build).filter_by(status="queued").count() == 1  # rebuilt on a fresh database
    bad = dict(form, edition="community", apps=["knowledge"])
    assert dev.post(f"/combos/{p.id}/settings", data=bad).status_code == 400


def test_board_and_lists_show_plain(s, user_client, ee):
    make_project(s)
    dev = user_client
    dev.post("/try", data=try_form(apps=[]))
    board = dev.get("/?project=combined").text
    assert "Combined &amp; plain" in board and ">plain<" in board and "Odoo 19.0 · Enterprise · demo · base only" in board
    projects = dev.get("/projects?tab=plain").text
    assert "Plain Odoo" in projects and "Odoo 19 EE + demo" in projects
    # registered-project choices for combined previews stay repo projects only
    combo_form = dev.get("/combos/new").text
    assert "Odoo 19 EE + demo" not in combo_form


def test_check_runs_on_plain(s, user_client, monkeypatch):
    """A plain preview goes through the same pre-build check (Enterprise source etc.)."""
    seen = []
    monkeypatch.setattr(accounts, "enterprise_status", lambda s, v, fresh=False: {
        "repo_url": "", "branch": v, "account": "", "ok": True, "message": ""})
    monkeypatch.setattr(builddeps, "check", lambda b, v=None: seen.append(b.project.kind) or [])
    dev = user_client
    dev.post("/try", data=try_form())
    p = s.query(Project).one()
    b = p.branches[0]
    b.last_commit_sha = "e" * 40
    s.commit()
    dev.post(f"/combos/{p.id}/settings", data={
        "name": p.name, "slug": p.slug, "default_odoo_version": "18.0", "edition": "enterprise",
        "install_demo_data": "with", "apps": ["crm"], "other_modules": ""})
    assert seen == ["plain"]
