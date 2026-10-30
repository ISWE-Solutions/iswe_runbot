"""Notebook tabs on long pages: System, build, branch, projects list, branch settings."""
import re

from previewer.models import Branch, Build, Job, Project, ProjectDependency

from test_web_app import as_role, make_branch, make_project  # noqa: F401


def active_tab(html: str) -> str:
    m = re.search(r'<a href="[^"]*[?&]tab=(\w+)" data-tab="\w+" aria-current="page"', html)
    return m.group(1) if m else ""


def client_tab(html: str) -> str:
    """The tab a client_tabs() bar starts on (rendered into its init script)."""
    m = re.search(r'show\(first \|\| "(\w+)", true\)', html)
    return m.group(1) if m else ""


def test_tabs_macro_marks_active_and_deep_links(s, as_role):
    html = as_role("admin").get("/admin/system?tab=tasks").text
    assert active_tab(html) == "tasks"
    for key in ("previews", "queue", "tasks", "templates", "settings", "jobs"):
        assert f'href="/admin/system?tab={key}"' in html


def test_system_tabs_render_only_their_section(s, as_role):
    c = as_role("admin")
    s.add(Job(type="sync", payload={"project_id": 1}, status="done"))
    s.commit()
    previews = c.get("/admin/system").text
    assert active_tab(previews) == "previews" and "Save settings" not in previews
    assert "Disk (data folder)" in previews  # summary cards stay above the tabs
    settings = c.get("/admin/system?tab=settings").text
    assert "Save settings" in settings and "Nothing waiting." not in settings
    jobs = c.get("/admin/system?tab=jobs&jtype=sync").text
    assert 'name="jtype"' in jobs and ">sync<" in jobs and "Save settings" not in jobs
    assert "Nothing waiting." in c.get("/admin/system?tab=queue").text


def test_system_settings_save_returns_to_settings_tab(s, as_role):
    from previewer import appsettings
    form = {k: v for k, v in appsettings.defaults().items() if k != "slack_webhook_url_enc"}
    form["max_running_previews"] = "20"
    r = as_role("admin").post("/admin/system/settings", data=form)
    assert r.status_code == 303 and r.headers["location"] == "/admin/system?tab=settings"


def test_build_page_default_tab(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    failed = Build(branch_id=b.id, commit_sha="a" * 40, odoo_version="18.0", status="failed",
                   error_summary="boom")
    running = Build(branch_id=b.id, commit_sha="b" * 40, odoo_version="18.0", status="running")
    s.add_all([failed, running])
    s.commit()
    c = as_role("tester")
    f = c.get(f"/builds/{failed.id}").text
    assert client_tab(f) == "why" and 'data-tab-panel="build:why"' in f and 'data-tab-panel="build:log"' in f
    r = c.get(f"/builds/{running.id}").text
    assert client_tab(r) == "log" and 'data-tab-panel="build:why"' not in r
    assert client_tab(c.get(f"/builds/{failed.id}?tab=details").text) == "details"


def test_branch_page_tabs(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    s.add(ProjectDependency(project=p, repo_url="https://github.com/acme/bank_extension", module_name="bank_zm"))
    s.add(Build(branch_id=b.id, commit_sha="a" * 40, odoo_version="18.0", status="failed"))
    s.commit()
    c = as_role("tester")
    overview = c.get(f"/branches/{b.id}").text
    assert active_tab(overview) == "overview" and "Last commit" in overview and "Delete selected" not in overview
    builds = c.get(f"/branches/{b.id}?tab=builds").text
    assert active_tab(builds) == "builds" and "Last commit" not in builds
    deps = c.get(f"/branches/{b.id}?tab=deps").text
    assert "bank_zm" in deps and "Chosen by" in deps
    assert 'id="live-plan"' in c.get(f"/branches/{b.id}?tab=plan").text


def test_projects_page_tabs_with_counts(s, as_role):
    make_project(s)
    s.add(Project(kind="combo", name="HR suite", slug="hr-suite", repo_url="", default_odoo_version="18.0"))
    s.commit()
    c = as_role("tester")
    html = c.get("/projects").text
    assert active_tab(html) == "projects" and "Payroll" in html and "HR suite" not in html
    assert re.search(r'Combined previews <span[^>]*>1</span>', html)
    combined = c.get("/projects?tab=combined").text
    assert "HR suite" in combined and "/projects/1\"" not in combined


def test_branch_settings_single_form_saves_every_tab(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    c = as_role("developer")
    page = c.get(f"/branches/{b.id}/settings").text
    assert page.count('<form method="post" class=') == 1  # one form (plus the sidebar's sign-out)
    for panel in ("bset:general", "bset:modules"):
        assert f'data-tab-panel="{panel}"' in page
    r = c.post(f"/branches/{b.id}/settings", data={
        "odoo_version_override": "19.0", "modules_mode": "custom", "modules_override": "payroll_zm",
        "demo_data_override": "on", "enabled": "on"})
    assert r.status_code == 303
    s.expire_all()
    b = s.get(Branch, b.id)
    assert (b.odoo_version_override, b.modules_override, b.demo_data_override, b.auto_rebuild) == \
        ("19.0", "payroll_zm", True, False)
