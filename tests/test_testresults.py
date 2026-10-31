"""testresults.parse() against real Odoo 18/19/20 test runs of tests/fixtures/test_modules."""
from pathlib import Path

import pytest

from previewer.diagnose import Source
from previewer.testresults import parse

LOGS = Path(__file__).parent / "fixtures" / "odoo_logs"
MOUNTS = {"/mnt/extra/0": Source("diag @ main", "https://github.com/acme/diag.git", "abc1234")}


@pytest.mark.parametrize("v", ["18.0", "19.0", "20.0"])
def test_failures_errors_and_skips(v):
    r = parse((LOGS / f"{v}-tests.log").read_text(), MOUNTS)
    assert r.ran and r.status == "failed" and (r.failed, r.errors) == (1, 1)
    assert r.headline.startswith("1 failed and 1 error(s) of ")
    by_test = {f.test: f for f in r.failures}
    fail, err = by_test["TestThings.test_fails"], by_test["TestThings.test_errors"]
    assert fail.kind == "fail" and fail.module == "diag_tests" and fail.line == 12
    assert fail.file == "diag_tests/tests/test_things.py"
    assert fail.url == "https://github.com/acme/diag/blob/abc1234/diag_tests/tests/test_things.py#L12"
    assert fail.message.startswith("AssertionError") and "Something else" in fail.message
    assert err.kind == "error" and err.line == 15 and "no_such_method" in err.message
    assert "Traceback" in err.traceback
    assert {"module": "diag_tests", "test": "TestHttp.test_tour",
            "reason": "websocket-client module is not installed"} in r.skipped_tests
    assert r.skipped == 1 and "diag_tests" in r.modules


def test_all_passing():
    r = parse((LOGS / "19.0-tests-pass.log").read_text())
    assert (r.status, r.total, r.passed, r.failures) == ("passed", 2, 2, [])
    assert r.headline == "2 test(s) passed"


def test_modules_without_tests():
    r = parse("2026-10-30 20:07:34,871 1 INFO db odoo.tests.result: 0 failed, 0 error(s) of 0 tests when loading database 'db'")
    assert r.status == "none" and r.headline == "These modules have no tests"


def test_no_tests_ran():
    r = parse((LOGS / "19.0-diag_xml_syntax.log").read_text())
    assert r.status is None and r.headline == "No tests ran"
    assert parse("").as_dict()["status"] is None
