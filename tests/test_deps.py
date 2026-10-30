from previewer import deps, github
from previewer import credentials
from previewer.models import Branch, GithubCredential, OdooVersion, Project, ProjectDependency
from previewer.security import encrypt


def _setup(s, edition="community"):
    s.add(GithubCredential(name="Default", token_enc=encrypt("tok"), is_default=True))
    s.get(OdooVersion, "18.0").enterprise_repo_url = "https://github.com/a/enterprise.git"
    s.get(OdooVersion, "19.0").enterprise_repo_url = "https://github.com/a/enterprise.git"
    p = Project(name="Payroll", slug="payroll", repo_url="https://github.com/a/payroll_extension",
                default_odoo_version="18.0", edition=edition)
    bank = ProjectDependency(project=p, repo_url="https://github.com/a/bank_extension.git",
                             module_name="bank_zm", branch_map={"18.0": "main"})
    other = ProjectDependency(project=p, repo_url="https://github.com/a/utils", position=1)
    b = Branch(project=p, name="v18-astro", slug="v18-astro", last_commit_sha="a" * 40)
    s.add_all([p, bank, other, b])
    s.commit()
    return p, bank, other, b


def test_resolve_sources(s):
    _, bank, other, b = _setup(s)
    got = [(r.dependency.root_module_name, r.branch, r.source) for r in deps.resolve(b)]
    assert got == [("bank_zm", "main", "project"), ("utils", "18.0", "default")]
    assert [r.branch for r in deps.resolve(b, "19.0")] == ["19.0", "19.0"]
    b.dependency_branches = {str(bank.id): "zra-frontline-changes"}
    s.commit()
    assert deps.resolve(b)[0].branch == "zra-frontline-changes"
    assert deps.resolve(b)[0].source == "branch"


def test_check_reports_missing_branches(s, monkeypatch):
    _, _, _, b = _setup(s, edition="enterprise")
    existing = {("bank_extension", "main"), ("enterprise", "18.0")}
    monkeypatch.setattr(github, "branch_exists",
                        lambda url, tok, br: (github.parse_repo(url)[1], br) in existing)
    res = {r.label: r for r in deps.check(b)}
    assert res["bank_zm"].ok and not res["utils"].ok
    assert "no branch '18.0'" in res["utils"].message
    assert res["Odoo Enterprise"].ok
    assert not {r.label: r for r in deps.check(b, "19.0")}["Odoo Enterprise"].ok


def test_enterprise_needs_from_manifests(s, monkeypatch):
    p, bank, other, b = _setup(s)
    files = {
        ("payroll_extension", "__manifest__.py"): "{'depends': ['base', 'hr_payroll', 'bank_zm']}",
        ("bank_extension", "__manifest__.py"): "{'depends': ['base']}",
    }
    monkeypatch.setattr(github, "get_file", lambda url, tok, path, ref:
                        files.get((github.parse_repo(url)[1], path)))
    monkeypatch.setattr(github, "top_level_dirs", lambda url, tok, ref:
                        ["hr_payroll", "web_studio"] if "enterprise" in url else [])
    p.module_name = "payroll_zm"
    needs = deps.enterprise_needs(s, p, "v18", "18.0", [(bank, "main")], "tok")
    assert needs == {"payroll_zm": ["hr_payroll"]}
    res = {r.label: r for r in deps.check(b)}
    assert not res["Odoo Community"].ok
    assert "payroll_zm depends on hr_payroll" in res["Odoo Community"].message


def test_credentials_resolution(s):
    p, bank, other, b = _setup(s)
    special = GithubCredential(name="Partner", username="odoo-partner", token_enc=encrypt("ptok"))
    s.add(special)
    s.commit()
    assert credentials.for_project(p).token == "tok"
    assert credentials.for_dependency(bank).token == "tok"
    bank.credential = special
    p.credential = None
    s.commit()
    assert credentials.for_dependency(bank).token == "ptok"
    assert "Partner" in credentials.for_dependency(bank).label and "ptok" not in credentials.for_dependency(bank).label
    v = s.get(OdooVersion, "19.0")
    v.enterprise_repo_url, v.enterprise_branch, v.enterprise_credential = (
        "https://github.com/odoo/enterprise.git", "", special)
    s.commit()
    src = credentials.enterprise_source(s, "19.0")
    assert (src.repo_url, src.branch, src.auth.token) == ("https://github.com/odoo/enterprise.git", "19.0", "ptok")
    assert credentials.enterprise_source(s, "18.0").auth.token == "tok"


def test_combo_commit_and_host():
    from previewer.naming import preview_host
    a = deps.combo_commit([("https://github.com/a/bank.git", "1" * 40), ("https://github.com/a/pay", "2" * 40)])
    b = deps.combo_commit([("https://github.com/a/pay/", "2" * 40), ("https://github.com/a/bank", "1" * 40)])
    assert a == b and len(a) == 40
    assert a != deps.combo_commit([("https://github.com/a/bank", "3" * 40), ("https://github.com/a/pay", "2" * 40)])
    assert preview_host("hr-suite", "combined", "preview.localhost", combo=True) == "hr-suite.preview.localhost"
