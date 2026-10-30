from previewer import modules

REPO = {
    "acme_base": ["base", "hr"],
    "acme_payroll": ["acme_base", "hr_payroll"],
    "acme_reports": ["acme_payroll", "acme_base"],
    "acme_portal": ["acme_base", "portal"],
    "acme_extra": ["acme_missing"],
}


def test_top_level_installs_everything():
    assert modules.top_level(REPO) == ["acme_extra", "acme_portal", "acme_reports"]
    p = modules.plan(["acme_reports", "acme_portal"], REPO, core={"hr", "portal", "hr_payroll"})
    assert set(p.install) == {"acme_reports", "acme_payroll", "acme_base", "acme_portal"}
    assert p.core == ["base", "hr", "hr_payroll", "portal"] and p.ok


def test_missing_dependency_explained():
    p = modules.plan(["acme_payroll", "acme_extra"], REPO, core={"hr"})
    assert p.missing == {"acme_payroll": ["hr_payroll"], "acme_extra": ["acme_missing"]}
    msg = modules.explain(p, enterprise_modules={"hr_payroll"}, edition="community")
    assert "acme_payroll cannot install: it depends on hr_payroll (an Odoo Enterprise module" in msg
    assert "acme_missing, which is not in the repo" in msg
    two = modules.plan(["acme_payroll"], {"acme_payroll": ["hr_payroll", "hr_payroll_account", "x"]}, core=set())
    msg = modules.explain(two, enterprise_modules={"hr_payroll", "hr_payroll_account"})
    assert msg == ("acme_payroll cannot install: it depends on hr_payroll and hr_payroll_account "
                   "(Odoo Enterprise modules: set the edition to Enterprise); and on x, which is "
                   "not in the repo, its dependencies or Odoo (add the repo that provides it as a dependency)")


def test_requested_module_not_anywhere():
    p = modules.plan(["nope"], REPO, core={"hr"})
    assert p.missing == {"nope": ["nope"]}
    assert modules.explain(p) == "nope: nope was asked for but is not in any repo of this build"


def test_unknown_core_assumes_core():
    assert modules.plan(["acme_payroll"], REPO, core=None).ok


def test_odoo_log_parsing():
    log = """2026-10-30 INFO demo odoo.modules.graph: module payroll_zm: Unmet dependencies: hr_payroll, bank_zm
2026-10-30 WARNING demo odoo.modules.module: module old_mod: not installable, skipped
2026-10-30 ERROR demo odoo.modules.loading: Some modules are not loaded, some dependencies or manifest may be missing: ['payroll_zm']
2026-10-30 WARNING demo odoo.modules.loading: invalid module names, ignored: does_not_exist"""
    msgs = modules.odoo_dependency_errors(log)
    assert msgs[0] == "payroll_zm was not installed: Odoo could not find its dependencies hr_payroll, bank_zm"
    assert any("old_mod is not installable" in m for m in msgs)
    assert any("Odoo did not load payroll_zm" in m for m in msgs)
    assert any("Unknown module name(s): does_not_exist" in m for m in msgs)
