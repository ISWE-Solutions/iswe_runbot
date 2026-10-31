"""Module test results for templates: badges for build cards/rows and the build page Tests tab
(data from Build.test_status / Build.test_results, written by the worker)."""
from ..models import Build

# Plain words for Odoo's skip reasons
SKIP_EXPLAINED = {
    "websocket-client module is not installed":
        "Browser test (tour): the Odoo image has no Chrome / websocket-client, so it was skipped.",
}


def badge(build: Build | None) -> dict | None:
    """{kind: pass|fail|none, label, title} or None when tests did not run / were off."""
    if build is None:
        return None
    r = build.test_results or {}
    status = build.test_status
    headline = r.get("headline") or ""
    if status == "passed":
        return {"kind": "pass", "label": f"✓ {r.get('passed', r.get('total', 0))}",
                "title": f"Tests: {headline}"}
    if status == "failed":
        bad = (r.get("failed") or 0) + (r.get("errors") or 0)
        return {"kind": "fail", "label": f"✗ {bad}", "title": f"Tests: {headline}"}
    if status == "none":
        return {"kind": "none", "label": "no tests", "title": "These modules have no tests"}
    return None


def warn_mode(build: Build) -> bool:
    """Tests failed but the preview still runs (test_failure_mode "warn")."""
    return build.test_status == "failed" and build.status in ("running", "sleeping", "stopped")


def failed_count(build: Build) -> int:
    r = build.test_results or {}
    return (r.get("failed") or 0) + (r.get("errors") or 0) if build.test_status == "failed" else 0


def state_message(build: Build) -> str | None:
    """Why there are no results to show, or None when there are."""
    r = build.test_results or {}
    if build.test_status in ("passed", "failed"):
        return None
    if build.test_status == "none":
        return "These modules have no tests."
    if build.test_status == "skipped":
        return "Tests were off for this build (or there were no modules of this project to test)."
    if build.status in ("queued", "building"):
        return "Tests run during the install; results appear when the build finishes."
    if build.status == "failed" and not r:
        return "Tests did not run: the install failed first (see Why it failed)."
    return "No test results were recorded for this build (it predates module tests, or tests were off)."


def skip_reason(reason: str) -> str:
    return SKIP_EXPLAINED.get(reason, reason)
