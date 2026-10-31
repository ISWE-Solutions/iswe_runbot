from previewer import appsettings, github
from previewer.models import Branch, Build, GithubCredential, Project
from previewer.security import encrypt
from previewer.worker import notify


def _build(s, *, post=True, kind="repo", status="running", diagnosis=None, previous=None,
           run_tests=False, test_status=None, test_results=None, previous_test_status=None):
    s.add(GithubCredential(name="Default", username="bot", token_enc=encrypt("tok"), is_default=True))
    p = Project(name="Payroll", slug="payroll", repo_url="https://github.com/acme/payroll_extension",
                default_odoo_version="19.0", kind=kind, post_commit_status=post, run_tests=run_tests)
    br = Branch(project=p, name="v19", slug="v19", host="payroll-v19.preview.localhost",
                last_commit_sha="c" * 40)
    s.add_all([p, br])
    s.flush()
    if previous:
        s.add(Build(branch_id=br.id, commit_sha="b" * 40, odoo_version="19.0", status=previous,
                    test_status=previous_test_status))
        s.flush()
    b = Build(branch_id=br.id, commit_sha="c" * 40, odoo_version="19.0", status=status,
              host=br.host, diagnosis=diagnosis or [], test_status=test_status,
              test_results=test_results or {})
    s.add(b)
    s.commit()
    return b


def _capture(monkeypatch):
    calls = {"status": [], "slack": []}
    monkeypatch.setattr(github, "create_commit_status",
                        lambda *a, timeout=5: calls["status"].append(a))

    class R:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(notify.httpx, "post", lambda url, json, timeout: calls["slack"].append((url, json)) or R())
    return calls


def test_commit_status_for_each_event(s, monkeypatch):
    calls = _capture(monkeypatch)
    b = _build(s, diagnosis=[{"title": "XML syntax error", "file": "views/x.xml", "line": 6,
                              "message": "Opening and ending tag mismatch\nmore"}])
    for event in ("building", "running", "failed"):
        notify.build_event(b.id, event)
    (u1, t1, sha, st1, ctx, d1, url1), (_, _, _, st2, _, d2, url2), (_, _, _, st3, _, d3, url3) = calls["status"]
    assert (u1, t1, sha, ctx) == ("https://github.com/acme/payroll_extension", "tok", "c" * 40, "odoo-preview/19.0")
    assert (st1, d1) == ("pending", "Building Odoo 19.0 preview") and url1.endswith(f"/builds/{b.id}")
    assert (st2, d2, url2) == ("success", "Preview running", "http://payroll-v19.preview.localhost")
    assert st3 == "failure" and d3 == "XML syntax error in views/x.xml:6: Opening and ending tag mismatch"
    assert calls["slack"] == []


def test_no_status_when_off_or_not_a_repo(s, monkeypatch):
    calls = _capture(monkeypatch)
    b = _build(s, post=False)
    notify.build_event(b.id, "running")
    assert calls["status"] == []


def test_slack_only_on_failure_and_fix(s, monkeypatch):
    calls = _capture(monkeypatch)
    appsettings.put(s, "slack_webhook_url_enc", encrypt("https://hooks.slack.test/T/B/x"))
    s.commit()
    b = _build(s, post=False, previous="failed", status="running")
    notify.build_event(b.id, "building")
    assert calls["slack"] == []
    notify.build_event(b.id, "running")
    assert len(calls["slack"]) == 1 and "fixed" in calls["slack"][0][1]["text"]
    assert calls["slack"][0][0] == "https://hooks.slack.test/T/B/x"
    b.status = "failed"
    b.error_summary = "Missing module dependency: foo"
    s.commit()
    notify.build_event(b.id, "failed")
    assert ":x:" in calls["slack"][-1][1]["text"] and "Missing module dependency" in calls["slack"][-1][1]["text"]


def test_running_after_success_does_not_ping_slack(s, monkeypatch):
    calls = _capture(monkeypatch)
    appsettings.put(s, "slack_webhook_url_enc", encrypt("https://hooks.slack.test/x"))
    s.commit()
    b = _build(s, post=False, previous="removed")
    notify.build_event(b.id, "running")
    assert calls["slack"] == []


def test_never_raises(s, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("github down")
    monkeypatch.setattr(github, "create_commit_status", boom)
    b = _build(s)
    notify.build_event(b.id, "running")  # logged, not raised
    notify.build_event(999999, "failed")  # unknown build
    monkeypatch.setattr(notify, "_send", boom)
    notify.build_event(b.id, "failed")


FAILED_TESTS = {"ran": True, "status": "failed", "headline": "1 failed and 1 error(s) of 6 tests"}


def test_tests_get_their_own_commit_status(s, monkeypatch):
    calls = _capture(monkeypatch)
    b = _build(s, run_tests=True, test_status="failed", test_results=FAILED_TESTS)
    notify.build_event(b.id, "building")
    notify.build_event(b.id, "running")
    by_ctx = [(c[4], c[3], c[5]) for c in calls["status"]]
    assert by_ctx[:2] == [("odoo-preview/19.0", "pending", "Building Odoo 19.0 preview"),
                          ("odoo-preview/19.0/tests", "pending", "Running module tests")]
    # warn mode: the preview is a success, the tests context a failure
    assert ("odoo-preview/19.0", "success", "Preview running · 1 failed and 1 error(s) of 6 tests") in by_ctx
    assert ("odoo-preview/19.0/tests", "failure", "1 failed and 1 error(s) of 6 tests") in by_ctx


def test_no_tests_context_when_tests_did_not_run(s, monkeypatch):
    calls = _capture(monkeypatch)
    b = _build(s, run_tests=False, test_status="skipped")
    notify.build_event(b.id, "running")
    assert [c[4] for c in calls["status"]] == ["odoo-preview/19.0"]


def test_slack_when_tests_newly_fail_and_recover(s, monkeypatch):
    calls = _capture(monkeypatch)
    appsettings.put(s, "slack_webhook_url_enc", encrypt("https://hooks.slack.test/x"))
    s.commit()
    b = _build(s, post=False, previous="removed", previous_test_status="passed",
               test_status="failed", test_results=FAILED_TESTS)
    notify.build_event(b.id, "running")
    assert len(calls["slack"]) == 1 and "module tests failed" in calls["slack"][0][1]["text"]
    b.test_status = "passed"
    b.test_results = {"ran": True, "status": "passed", "headline": "6 test(s) passed"}
    prev = next(x for x in b.branch.builds if x.id < b.id)
    prev.test_status = "failed"
    s.commit()
    notify.build_event(b.id, "running")
    assert "pass again" in calls["slack"][-1][1]["text"]
