"""diagnose() against real Odoo 18/19 failure logs (tests/fixtures/odoo_logs, produced by
installing deliberately broken modules)."""
from pathlib import Path

import pytest

from previewer.diagnose import Signals, Source, diagnose, headline

LOGS = Path(__file__).parent / "fixtures" / "odoo_logs"
MOUNTS = {"/mnt/extra/0": Source("diag @ main", "https://github.com/acme/diag.git", "abc1234")}


def first(version: str, case: str):
    found = diagnose((LOGS / f"{version}-diag_{case}.log").read_text(), MOUNTS)
    assert found, f"nothing found for {version} {case}"
    return found[0]


@pytest.mark.parametrize("v", ["18.0", "19.0", "20.0"])
def test_xml_syntax(v):
    f = first(v, "xml_syntax")
    assert f.kind == "xml_syntax" and "Opening and ending tag mismatch" in f.message
    assert (f.module, f.file, f.line) == ("diag_xml_syntax", "diag_xml_syntax/views/v.xml", 6)
    assert f.url == "https://github.com/acme/diag/blob/abc1234/diag_xml_syntax/views/v.xml#L6"


@pytest.mark.parametrize("v", ["18.0", "19.0", "20.0"])
def test_view_field(v):
    f = first(v, "view_field")
    assert f.kind == "view" and 'Field "x_does_not_exist" does not exist in model "res.partner"' in f.message
    assert f.file == "diag_view_field/views/v.xml" and f.line == 2
    assert "x_does_not_exist" in f.hint and "(res.partner)" in f.title and "<form" in f.excerpt


@pytest.mark.parametrize("v", ["18.0", "19.0", "20.0"])
def test_external_id(v):
    f = first(v, "xmlid")
    assert f.kind == "external_id" and "nonexistent_module.nonexistent_record" in f.message
    assert f.file == "diag_xmlid/data/d.xml" and f.line == 2


@pytest.mark.parametrize("v", ["18.0", "19.0", "20.0"])
def test_bad_field_in_data(v):
    f = first(v, "bad_field_data")
    assert f.kind == "xml_data" and "Invalid field 'x_no_such_field' on model 'res.partner'" in f.message


@pytest.mark.parametrize("v", ["18.0", "19.0"])
def test_access_csv(v):
    f = first(v, "access")
    assert f.kind == "access_csv" and f.module == "diag_access"
    assert "model_does_not_exist" in f.message and f.file == "security/ir.model.access.csv"


@pytest.mark.parametrize("v", ["18.0", "19.0", "20.0"])
def test_python_syntax(v):
    f = first(v, "py_syntax")
    assert f.kind == "python_syntax" and "expected ':'" in f.message
    assert (f.file, f.line) == ("diag_py_syntax/models/m.py", 3)


@pytest.mark.parametrize("v", ["18.0", "19.0", "20.0"])
def test_python_import(v):
    f = first(v, "py_import")
    assert f.kind == "python_import" and "notarealpkg2" in f.message and "requirements.txt" in f.hint
    assert f.file == "diag_py_import/models/m.py" and f.line == 1


@pytest.mark.parametrize("v", ["18.0", "19.0", "20.0"])
def test_external_python_dependency(v):
    f = first(v, "py_lib")
    assert f.kind == "python_package" and f.module == "diag_py_lib" and "notarealpkg" in f.hint


@pytest.mark.parametrize("v", ["18.0", "19.0", "20.0"])
def test_missing_module_dependency(v):
    f = first(v, "missing_dep")
    assert f.kind == "missing_dependency" and "no_such_module" in f.message


def test_unknown_comodel_on_19():
    f = first("19.0", "model_error")
    assert f.kind == "model_definition" and "res.no_such_model" in f.message


def test_root_module_repo_paths_and_link():
    mounts = {"/mnt/src": Source("payroll_extension @ v18", "https://github.com/ISWE-Solutions/payroll_extension",
                                 "6f2e1b0", root_module="payroll_zm")}
    log = ('  File "/mnt/src/payroll_zm/views/hr.xml", line 12\n'
           "lxml.etree.XMLSyntaxError: Premature end of data, line 12, column 1\n")
    f = diagnose(log, mounts)[0]
    assert (f.module, f.file, f.source) == ("payroll_zm", "views/hr.xml", "payroll_extension @ v18")
    assert f.url == "https://github.com/ISWE-Solutions/payroll_extension/blob/6f2e1b0/views/hr.xml#L12"
    assert headline([f]) == ("XML syntax error in views/hr.xml:12 (payroll_extension @ v18): "
                             "Premature end of data, line 12, column 1")


def test_signals_and_fallback():
    assert diagnose("", signals=Signals(oom_killed=True, memory_limit="2g"))[0].kind == "out_of_memory"
    assert diagnose("", signals=Signals(timed_out=True, timeout_minutes=20))[0].message.endswith("20 minutes.")
    assert diagnose("all fine") == []
    other = diagnose("Traceback (most recent call last):\n  x\nKeyError: 'boom'\n")[0]
    assert other.kind == "other" and "KeyError" in other.message


def test_odoo_listening_on_loopback_only():
    log = ("2026-10-30 19:38:16,159 1 INFO ? odoo.service.server: HTTP service running on 127.0.0.1:8069\n"
           "Preview did not become healthy: health check /web/login unreachable (ConnectError) after 120s\n")
    f = diagnose(log, signals=Signals(health_failed=True))[0]
    assert f.kind == "health" and "127.0.0.1:8069" in f.message and "http_interface = 0.0.0.0" in f.hint


def test_access_csv_odoo20_unknown_model():
    f = first("20.0", "access")
    assert f.kind == "access_csv" and f.module == "diag_access"
    assert f.file == "security/ir.model.access.csv" and "ir.model.access" in f.message
    assert "'depends'" in f.hint
