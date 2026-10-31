"""Outside notifications about builds (GitHub commit statuses, Slack). Called by the
pipeline; must never raise, and gives up after a few seconds so a slow GitHub or Slack never
holds a build slot."""
import logging

import httpx

from .. import appsettings, credentials, github
from ..config import get_settings
from ..db import session_scope
from ..models import Build
from ..security import decrypt

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
        slack_enc = appsettings.get(s, "slack_webhook_url_enc")
        slack_text = None
        if slack_enc and event == "failed":
            slack_text = (f":x: {project.name} / {b.branch.name}: build #{b.id} failed on Odoo "
                          f"{b.odoo_version}\n{headline(b)}\n{build_url(b.id)}")
        elif slack_enc and event == "running" and _previous_failed(s, b):
            url = get_settings().preview_url(b.host) if b.host else build_url(b.id)
            slack_text = (f":white_check_mark: {project.name} / {b.branch.name}: fixed, build #{b.id} "
                          f"is running on Odoo {b.odoo_version}\n{url}")
        elif slack_enc and event == "running" and _test_change(b) == "failed":
            slack_text = (f":warning: {project.name} / {b.branch.name}: module tests failed on build "
                          f"#{b.id} ({tests}); the preview is running\n{build_url(b.id)}")
        elif slack_enc and event == "running" and _test_change(b) == "recovered":
            slack_text = (f":white_check_mark: {project.name} / {b.branch.name}: module tests pass "
                          f"again on build #{b.id} ({tests})\n{build_url(b.id)}")
    for status in statuses:
        try:
            github.create_commit_status(*status, timeout=TIMEOUT)
        except Exception as e:  # noqa: BLE001
            log.warning("commit status for build %s not posted: %s", build_id, e)
    if slack_text:
        try:
            r = httpx.post(decrypt(slack_enc), json={"text": slack_text}, timeout=TIMEOUT)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            log.warning("Slack message for build %s not sent: %s", build_id, type(e).__name__)
