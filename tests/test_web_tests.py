"""Module tests in the dashboard: settings, build page Tests tab, badges."""
from pathlib import Path

from previewer.diagnose import Source
from previewer.models import Branch, Build, Project
from previewer.testresults import parse

from test_web_app import as_role, client, make_branch, make_project  # noqa: F401

LOG = Path(__file__).parent / "fixtures" / "odoo_logs" / "19.0-tests.log"
MOUNTS = {"/mnt/extra/0": Source("diag @ main", "https://github.com/acme/diag.git", "abc1234")}


def make_tested_build(s, b, status="running", test_status="failed") -> Build:
    report = parse(LOG.read_text(), MOUNTS).as_dict()
    build = Build(branch_id=b.id, commit_sha="a" * 40, odoo_version="18.0", status=status,
                  host=b.host, test_status=test_status, test_results=report if test_status == "failed" else
                  ({"ran": True, "total": 42, "passed": 42, "failed": 0, "errors": 0, "skipped": 0,
                    "status": "passed", "headline": "42 test(s) passed", "failures": [], "skipped_tests": [],
                    "modules": ["payroll_zm"]} if test_status == "passed" else {}))
    s.add(build)
    s.commit()
    return build


def test_project_test_settings_save_and_validate(s, as_role):
    p = make_project(s)
    assert p.run_tests is True and p.test_failure_mode == "warn"  # defaults: on, warn
    c = as_role("admin")
    page = c.get(f"/projects/{p.id}?tab=settings").text
    assert "Run module tests during the build" in page and 'value="warn"' in page
    r = c.post(f"/projects/{p.id}/tests", data={"test_failure_mode": "fail", "test_tags": "/payroll_zm,-/payroll_zm:slow"})
    assert r.status_code == 303
    s.expire_all()
    p = s.get(Project, p.id)
    assert (p.run_tests, p.test_failure_mode, p.test_tags) == (False, "fail", "/payroll_zm,-/payroll_zm:slow")
    r = c.post(f"/projects/{p.id}/tests", data={"run_tests": "on", "test_tags": "rm -rf /;"}, follow_redirects=True)
    assert "Invalid test tag" in r.text
    s.expire_all()
    assert s.get(Project, p.id).run_tests is False  # rejected save changed nothing


def test_project_test_settings_permissions(s, as_role):
    p = make_project(s)
    assert as_role("developer").post(f"/projects/{p.id}/tests", data={"run_tests": "on"}).status_code == 403
    assert as_role("tester").post(f"/projects/{p.id}/tests", data={"run_tests": "on"}).status_code == 403


def test_plain_previews_have_no_test_settings(s, as_role):
    p = make_project(s)
    p.kind, p.repo_url = "plain", ""
    s.commit()
    b = make_branch(s, p, name="combined", slug="combined")
    c = as_role("admin")
    assert "Run module tests" not in c.get(f"/combos/{p.id}?tab=settings").text
    assert c.post(f"/projects/{p.id}/tests", data={"run_tests": "on"}).status_code == 404
    assert "Module tests:" not in c.get(f"/combos/{p.id}").text
    assert b


def test_branch_override_rebuilds(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    c = as_role("developer")
    assert "Project default (run)" in c.get(f"/branches/{b.id}/settings").text
    r = c.post(f"/branches/{b.id}/settings", data={"run_tests_override": "off", "enabled": "on"})
    assert r.status_code == 303
    s.expire_all()
    b = s.get(Branch, b.id)
    assert b.run_tests_override is False and b.effective_run_tests is False
    assert s.query(Build).filter_by(status="queued").count() == 1


def test_tests_tab_shows_failures_links_traceback_and_skips(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    build = make_tested_build(s, b)
    c = as_role("tester")
    page = c.get(f"/builds/{build.id}").text  # running with failed tests -> Tests tab first
    assert 'data-tab="tests" aria-selected="true"' in page
    assert "TestThings.test_fails" in page and "TestThings.test_errors" in page
    assert "https://github.com/acme/diag/blob/abc1234/diag_tests/tests/test_things.py#L12" in page
    assert "Something else" in page and "Traceback" in page and ">FAIL<" in page and ">ERROR<" in page
    assert "Browser test (tour)" in page  # websocket-client skip explained
    assert "The preview still runs" in page


def test_tests_tab_states_and_default_tab(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    ok = make_tested_build(s, b, test_status="passed")
    c = as_role("tester")
    page = c.get(f"/builds/{ok.id}").text
    assert 'data-tab="log" aria-selected="true"' in page and "42 test(s) passed" in page
    off = Build(branch_id=b.id, commit_sha="b" * 40, odoo_version="18.0", status="running", test_status="skipped")
    failed = Build(branch_id=b.id, commit_sha="c" * 40, odoo_version="18.0", status="failed", error_summary="boom")
    none = Build(branch_id=b.id, commit_sha="d" * 40, odoo_version="18.0", status="running", test_status="none")
    s.add_all([off, failed, none])
    s.commit()
    assert "Tests were off for this build" in c.get(f"/builds/{off.id}").text
    page = c.get(f"/builds/{failed.id}").text
    assert 'data-tab="why" aria-selected="true"' in page and "the install failed first" in page
    assert "These modules have no tests" in c.get(f"/builds/{none.id}").text


def test_board_and_rows_show_test_badges(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    b.enabled, b.sticky = True, True
    s.commit()
    make_tested_build(s, b, test_status="passed")
    c = as_role("tester")
    board = c.get("/?project=all").text
    assert "✓ 42" in board and "Tests: 42 test(s) passed" in board
    bad = make_tested_build(s, b)
    board = c.get("/?project=all").text
    assert "✗ 2" in board and "border-amber-300" in board  # warn mode: preview still runs
    rows = c.get(f"/projects/{p.id}?tab=branches&sel=all").text
    assert "✗ 2" in rows
    overview = c.get(f"/branches/{b.id}").text
    assert "Module tests:" in overview and f"/builds/{bad.id}?tab=tests" in overview
