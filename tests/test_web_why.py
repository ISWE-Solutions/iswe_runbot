"""'Why it failed': build page panel, on-the-fly diagnosis of old logs, card summaries."""
from pathlib import Path

from previewer.config import get_settings
from previewer.models import Build, ProjectDependency
from previewer.web import failure_view

from test_web_app import make_branch, make_project, user_client  # noqa: F401  (fixture + helpers)

FIXTURES = Path(__file__).parent / "fixtures" / "odoo_logs"

FINDING = {
    "kind": "view", "title": "Invalid view v1 (res.partner)",
    "message": 'Field "x_does_not_exist" does not exist in model "res.partner"',
    "hint": "The view uses field x_does_not_exist, which model res.partner does not have.",
    "module": "payroll_zm", "file": "views/hr.xml", "line": 12, "source": "acme/payroll_extension @ v18",
    "url": "https://github.com/acme/payroll_extension/blob/abc/views/hr.xml#L12",
    "excerpt": '<form><field name="x_does_not_exist"/></form>',
}


def _failed(s, b, **kw):
    build = Build(branch_id=b.id, commit_sha="a" * 40, odoo_version="18.0", status="failed",
                  error_summary=kw.pop("error_summary", "Invalid view v1"), **kw)
    s.add(build)
    s.commit()
    return build


def test_build_page_shows_saved_findings(s, user_client):
    p = make_project(s)
    b = make_branch(s, p)
    build = _failed(s, b, diagnosis=[FINDING])
    r = user_client.get(f"/builds/{build.id}")
    assert r.status_code == 200 and "Why it failed" in r.text
    assert FINDING["url"] in r.text and "views/hr.xml:12" in r.text
    assert "How to fix:" in r.text and "x_does_not_exist" in r.text and "Jump to this line in the log" in r.text
    assert "&lt;form&gt;&lt;field" in r.text  # excerpt escaped


def test_old_build_is_diagnosed_from_its_log(s, user_client, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    p = make_project(s, edition="community")
    b = make_branch(s, p)
    s.add(ProjectDependency(project_id=p.id, repo_url="https://github.com/acme/diag.git", module_name=""))
    s.commit()
    build = _failed(s, b, dependency_refs=[{"name": "diag", "repo_url": "https://github.com/acme/diag.git",
                                            "branch": "main", "sha": "f00d123", "source": "project"}])
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / f"{build.id}.log").write_text((FIXTURES / "19.0-diag_view_field.log").read_text())
    found = failure_view.findings_for(build)
    assert found[0]["kind"] == "view" and found[0]["file"] == "diag_view_field/views/v.xml"
    assert found[0]["url"] == "https://github.com/acme/diag/blob/f00d123/diag_view_field/views/v.xml#L2"
    r = user_client.get(f"/builds/{build.id}")
    assert "x_does_not_exist" in r.text and "diag_view_field/views/v.xml:2" in r.text


def test_summary_fallback_for_setup_failures(s, user_client, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    p = make_project(s)
    b = make_branch(s, p)
    build = _failed(s, b, error_summary="Odoo Enterprise 19.0 could not be fetched: boom")
    f = failure_view.findings_for(build)[0]
    assert f["kind"] == "setup" and "Odoo versions page" in f["hint"]


def test_no_panel_for_running_build(s, user_client):
    p = make_project(s)
    b = make_branch(s, p)
    build = Build(branch_id=b.id, commit_sha="a" * 40, odoo_version="18.0", status="running", diagnosis=[FINDING])
    s.add(build)
    s.commit()
    assert "Why it failed" not in user_client.get(f"/builds/{build.id}").text


def test_board_card_and_branch_page_show_finding(s, user_client):
    p = make_project(s)
    b = make_branch(s, p)
    b.enabled = True
    s.commit()
    build = _failed(s, b, diagnosis=[FINDING])
    c = user_client
    board = c.get("/board/rows?project=all&show=all").text
    assert "Invalid view v1 (res.partner)" in board and "views/hr.xml:12" in board
    page = c.get(f"/branches/{b.id}").text
    assert "Why it failed · build #%d" % build.id in page and "Invalid view v1" in page


def test_mounts_for_root_module_project(s):
    p = make_project(s, module_name="payroll_zm")
    b = make_branch(s, p)
    build = _failed(s, b, dependency_refs=[{"name": "bank_zm", "repo_url": "https://github.com/acme/bank_extension.git",
                                            "branch": "main", "sha": "a9a6486"}])
    m = failure_view.mounts_for(build)
    assert m["/mnt/src"].root_module == "payroll_zm" and m["/mnt/src"].ref == "a" * 40
    assert m["/mnt/extra/0"].label == "acme/bank_extension @ main" and m["/mnt/extra/0"].ref == "a9a6486"
    from previewer.diagnose import diagnose
    log = '  File "/mnt/src/payroll_zm/views/hr.xml", line 4\nlxml.etree.XMLSyntaxError: Premature end of data, line 4\n'
    f = diagnose(log, m)[0]
    assert f.file == "views/hr.xml" and f.url.endswith("/acme/payroll_extension/blob/" + "a" * 40 + "/views/hr.xml#L4")
