"""Outside notifications about builds (GitHub commit statuses, and alerts to Slack, Teams,
Discord, Google Chat, a webhook and email via alerts.py). Called by the pipeline; must never
raise, and gives up after a few seconds so a slow service never holds a build slot."""
import logging

from .. import alerts, credentials, github
from ..config import get_settings
from ..db import session_scope
from ..models import Build

log = logging.getLogger("previewer.notify")

EVENTS = ("building", "running", "failed")
TIMEOUT = 5
_STATE = {"building": "pending", "running": "success", "failed": "failure"}


def build_event(build_id: int, event: str) -> None:
    try:
        _send(build_id, event)
    except Exception:  # noqa: BLE001 — notifications never break a build
        log.exception("notification for build %s (%s) failed", build_id, event)


def headline(b: Build) -> str:
    if b.diagnosis:
        f = b.diagnosis[0]
        where = f" in {f['file']}:{f['line']}" if f.get("file") and f.get("line") else ""
        msg = (f.get("message") or "").strip().splitlines()
        return f"{f.get('title', 'Failed')}{where}" + (f": {msg[0]}" if msg else "")
    return ((b.error_summary or "Build failed").splitlines() or ["Build failed"])[0]


def build_url(build_id: int) -> str:
    return f"{get_settings().dashboard_url}/builds/{build_id}"


def _previous(b: Build) -> Build | None:
    return next((x for x in b.branch.builds if x.id < b.id
                 and x.error_summary != "Superseded by a newer build"), None)


def _previous_failed(s, b: Build) -> bool:
    prev = _previous(b)
    return prev is not None and prev.status == "failed"


def test_headline(b: Build) -> str | None:
    """"2 failed of 9 tests" etc., or None when no tests ran for this build."""
    r = b.test_results or {}
    return r.get("headline") if r.get("ran") else None


def _test_change(b: Build) -> str | None:
    """"failed" when tests newly fail, "recovered" when they pass again, else None."""
    prev = _previous(b)
    before = prev.test_status if prev else None
    if b.test_status == "failed" and before != "failed":
        return "failed"
    if b.test_status == "passed" and before == "failed":
        return "recovered"
    return None


def _send(build_id: int, event: str) -> None:
    if event not in EVENTS:
        return
    with session_scope() as s:
        b = s.get(Build, build_id)
        if b is None:
            return
        project = b.branch.project
        statuses = []
        tests = test_headline(b)
        if project.kind == "repo" and project.post_commit_status and b.commit_sha:
            token = credentials.for_project(project).token
            if event == "building":
                desc = f"Building Odoo {b.odoo_version} preview"
            elif event == "running":
                desc = "Preview running" + (f" · {tests}" if tests else "")
            else:
                desc = headline(b)
            target = get_settings().preview_url(b.host) if event == "running" and b.host else build_url(b.id)
            statuses.append((project.repo_url, token, b.commit_sha, _STATE[event],
                             f"odoo-preview/{b.odoo_version}", desc, target))
            # A second context for module tests, so a warn-mode failure still shows on the PR
            tests_ctx = f"odoo-preview/{b.odoo_version}/tests"
            if event == "building" and b.branch.effective_run_tests:
                statuses.append((project.repo_url, token, b.commit_sha, "pending", tests_ctx,
                                 "Running module tests", build_url(b.id)))
            elif event in ("running", "failed") and tests:
                state = "failure" if b.test_status == "failed" else "success"
                statuses.append((project.repo_url, token, b.commit_sha, state, tests_ctx, tests,
                                 build_url(b.id)))
        alert = _alert(s, b, event, tests)
        channels = alerts.targets(s) if alert else []
    for status in statuses:
        try:
            github.create_commit_status(*status, timeout=TIMEOUT)
        except Exception as e:  # noqa: BLE001
            log.warning("commit status for build %s not posted: %s", build_id, e)
    if channels:
        alerts.deliver(channels, alert)


def _alert(s, b: Build, event: str, tests: str | None) -> alerts.Alert | None:
    """Failures, fixes, and module tests newly failing or passing again; None otherwise."""
    project, where = b.branch.project, f"{b.branch.project.name} / {b.branch.name}"
    common = {"project": project.name, "branch": b.branch.name, "build_id": b.id, "odoo_version": b.odoo_version}
    if event == "failed":
        return alerts.Alert("failed", f"{where}: build #{b.id} failed on Odoo {b.odoo_version}",
                            build_url(b.id), headline(b), **common)
    if event != "running":
        return None
    if _previous_failed(s, b):
        url = get_settings().preview_url(b.host) if b.host else build_url(b.id)
        return alerts.Alert("fixed", f"{where}: fixed, build #{b.id} is running on Odoo {b.odoo_version}",
                            url, **common)
    change = _test_change(b)
    if change == "failed":
        return alerts.Alert("tests_failed", f"{where}: module tests failed on build #{b.id} ({tests}); "
                            "the preview is running", build_url(b.id), **common)
    if change == "recovered":
        return alerts.Alert("tests_recovered", f"{where}: module tests pass again on build #{b.id} ({tests})",
                            build_url(b.id), **common)
    return None
