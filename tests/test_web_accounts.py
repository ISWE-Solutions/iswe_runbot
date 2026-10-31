"""Git credentials page, account choices on projects/dependencies, Enterprise sources."""
import pytest

from previewer import github
from previewer.models import GithubCredential, OdooVersion, Project, ProjectDependency
from previewer.security import decrypt
from previewer.web import accounts as web_accounts

from test_web_app import PROJECT_FORM, make_account, make_project, user_client  # noqa: F401


@pytest.fixture(autouse=True)
def fresh_cache():
    web_accounts.clear_enterprise_cache()
    yield


def acc(s, name):
    return s.query(GithubCredential).filter_by(name=name).one()


def test_account_crud_single_default_and_token_hidden(s, user_client):
    c = user_client
    r = c.post("/admin/github-accounts/new", data={"name": "Default", "username": "TBS5596", "token": "ghp_first111"})
    assert r.status_code == 303
    first = acc(s, "Default")
    assert first.is_default and decrypt(first.token_enc) == "ghp_first111"     # first one = default
    c.post("/admin/github-accounts/new", data={"name": "Partner", "username": "odoo-p", "token": "ghp_second222"})
    partner = acc(s, "Partner")
    assert not partner.is_default

    page = c.get("/admin/github-accounts").text
    assert "Default" in page and "Partner" in page and "ghp_first111" not in page and "ghp_second222" not in page
    edit = c.get(f"/admin/github-accounts/{partner.id}/edit").text
    assert "ghp_second222" not in edit and "Stored" in edit

    # Blank token on edit keeps it
    c.post(f"/admin/github-accounts/{partner.id}/edit", data={"name": "Partner 2", "username": "odoo-p", "token": ""})
    s.expire_all()
    assert acc(s, "Partner 2").token_enc and decrypt(acc(s, "Partner 2").token_enc) == "ghp_second222"

    dup = c.post("/admin/github-accounts/new", data={"name": "Default", "token": "x"})
    assert dup.status_code == 400 and "Another account has this name" in dup.text
    missing = c.post("/admin/github-accounts/new", data={"name": "No token", "token": ""})
    assert missing.status_code == 400

    c.post(f"/admin/github-accounts/{partner.id}/default")
    s.expire_all()
    assert [a.name for a in s.query(GithubCredential).filter_by(is_default=True)] == ["Partner 2"]


def test_delete_refused_when_default_or_used(s, user_client):
    p = make_project(s)                                   # creates "Default"
    other = make_account(s, name="Other", default=False)
    p.credential_id = other.id
    s.commit()
    c = user_client
    r = c.post(f"/admin/github-accounts/{acc(s, 'Default').id}/delete", follow_redirects=True)
    assert "is the default account" in r.text
    r = c.post(f"/admin/github-accounts/{other.id}/delete", follow_redirects=True)
    assert "still used by project Payroll" in r.text
    p.credential_id = None
    s.commit()
    c.post(f"/admin/github-accounts/{other.id}/delete")
    s.expire_all()
    assert s.query(GithubCredential).filter_by(name="Other").count() == 0


def test_test_stores_login_and_warns_on_username(s, user_client, monkeypatch):
    a = make_account(s, username="someone-else")
    monkeypatch.setattr(web_accounts, "whoami", lambda token: {
        "login": "TBS5596", "rate_remaining": "4990", "rate_limit": "5000", "scopes": None})
    monkeypatch.setattr(github, "test_connection", lambda url, token: {
        "full_name": "odoo/enterprise", "private": True, "default_branch": "19.0"})
    r = user_client.post(f"/admin/github-accounts/{a.id}/test", data={"repo_url": "odoo/enterprise"})
    assert "Authenticated as TBS5596" in r.text and "4990 of 5000" in r.text
    assert "token belongs to TBS5596" in r.text and "Can read odoo/enterprise" in r.text
    s.expire_all()
    a = s.get(GithubCredential, a.id)
    assert a.checked_login == "TBS5596" and a.checked_at and not a.check_error


def test_test_stores_error(s, user_client):
    a = make_account(s)                                   # GitHub is offline in tests
    r = user_client.post(f"/admin/github-accounts/{a.id}/test", data={})
    assert "✗" in r.text
    s.expire_all()
    assert s.get(GithubCredential, a.id).check_error


def test_test_connection_uses_chosen_account(s, user_client, monkeypatch):
    make_account(s, token="default-tok")
    partner = make_account(s, name="Partner", token="partner-tok", default=False)
    seen = []
    monkeypatch.setattr(github, "test_connection", lambda url, token: seen.append(token) or {
        "full_name": "acme/x", "private": True, "default_branch": "main"})
    monkeypatch.setattr(github, "list_branches", lambda url, token: [])
    c = user_client
    r = c.post("/projects/test-connection", data={"repo_url": "https://github.com/acme/x", "credential_id": ""})
    assert "with Default" in r.text
    c.post("/projects/test-connection", data={"repo_url": "https://github.com/acme/x", "credential_id": str(partner.id)})
    assert seen == ["default-tok", "partner-tok"]


def test_dependency_lookups_use_chosen_account(s, user_client, monkeypatch):
    p = make_project(s)
    partner = make_account(s, name="Partner", token="partner-tok", default=False)
    seen = []
    monkeypatch.setattr(github, "test_connection", lambda url, token: seen.append(token) or {
        "full_name": "acme/bank", "private": True, "default_branch": "main"})
    c = user_client
    c.post(f"/projects/{p.id}/deps/test", data={"repo_url": "https://github.com/acme/bank", "credential_id": ""})
    c.post(f"/projects/{p.id}/deps/test", data={"repo_url": "https://github.com/acme/bank",
                                               "credential_id": str(partner.id)})
    assert seen == ["tok", "partner-tok"]


def test_version_enterprise_source_save_and_check(s, user_client, monkeypatch):
    make_account(s)
    partner = make_account(s, name="Partner", token="partner-tok", default=False)
    calls = []

    def exists(url, token, branch):
        calls.append((url, token, branch))
        if "odoo/enterprise" in url:
            return branch == "19.0"
        raise github.GitHubError("ISWE-Solutions/enterprise not found, or the token cannot see it (404)")

    monkeypatch.setattr(github, "branch_exists", exists)
    c = user_client
    form = {"enterprise_repo_url": "github.com/odoo/enterprise", "enterprise_branch": "",
            "enterprise_credential_id": str(partner.id)}
    r = c.post("/admin/versions/19.0/enterprise/check", data=form)
    assert "✓ odoo/enterprise @ 19.0 is readable with credential &#39;Partner&#39;" in r.text
    s.expire_all()
    assert s.get(OdooVersion, "19.0").enterprise_repo_url == ""          # check never saves

    r = c.post("/admin/versions/19.0/enterprise", data=form, follow_redirects=True)
    s.expire_all()
    v = s.get(OdooVersion, "19.0")
    assert v.enterprise_repo_url == "https://github.com/odoo/enterprise.git"
    assert v.enterprise_credential_id == partner.id and v.enterprise_branch == ""
    assert ("https://github.com/odoo/enterprise.git", "partner-tok", "19.0") in calls
    page = c.get("/admin/versions").text
    assert "✓ odoo/enterprise @ 19.0" in page

    r = c.post("/admin/versions/18.0/enterprise/check", data={
        "enterprise_repo_url": "https://github.com/ISWE-Solutions/enterprise", "enterprise_branch": "",
        "enterprise_credential_id": ""})
    assert "Cannot read ISWE-Solutions/enterprise with credential &#39;Default&#39;" in r.text
    bad = c.post("/admin/versions/18.0/enterprise", data={"enterprise_repo_url": "not a url"}, follow_redirects=True)
    assert "not saved" in bad.text
