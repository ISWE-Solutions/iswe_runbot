import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from previewer.config import get_settings
from previewer.models import GithubCredential, Branch, Build, Job, Project, User
from previewer.security import decrypt, encrypt, hash_password
from previewer.web.main import app

PASSWORD = "correct-horse-1"


def make_user(s, role="admin", email=None):
    u = User(email=email or f"{role}@example.com", name=role.title(), role=role,
             password_hash=hash_password(PASSWORD))
    s.add(u)
    s.commit()
    return u


def client() -> TestClient:
    return TestClient(app, base_url="http://preview.localhost", follow_redirects=False)


def csrf_of(c: TestClient, path="/login") -> str:
    html = c.get(path).text
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(c: TestClient, email: str) -> str:
    token = csrf_of(c)
    r = c.post("/login", data={"email": email, "password": PASSWORD, "csrf_token": token, "next": "/"})
    assert r.status_code == 303, r.text
    return token


@pytest.fixture
def as_role(s):
    def _make(role: str):
        u = make_user(s, role)
        c = client()
        tok = login(c, u.email)
        c.headers["X-CSRF-Token"] = tok
        return c
    return _make


def make_account(s, name="Default", token="tok", default=True, username="") -> GithubCredential:
    c = GithubCredential(name=name, username=username, token_enc=encrypt(token), is_default=default)
    s.add(c)
    s.commit()
    return c


def make_project(s, **kw) -> Project:
    from previewer import credentials
    if credentials.default(s) is None:
        make_account(s)
    p = Project(name="Payroll", slug="payroll", repo_url="https://github.com/acme/payroll_extension",
                default_odoo_version="18.0", **kw)
    s.add(p)
    s.commit()
    return p


def make_branch(s, p, name="v18", slug="v18", sha="a" * 40) -> Branch:
    b = Branch(project_id=p.id, name=name, slug=slug, host=f"{p.slug}-{slug}.preview.localhost",
               last_commit_sha=sha, last_commit_message="Fix tax", last_commit_author="Dev")
    s.add(b)
    s.commit()
    return b


# ---------- auth ----------

def test_login_logout(s):
    make_user(s, "tester")
    c = client()
    assert c.get("/").headers["location"].startswith("/login")
    token = csrf_of(c)
    bad = c.post("/login", data={"email": "tester@example.com", "password": "nope", "csrf_token": token})
    assert bad.status_code == 400 and "Wrong email, username or password" in bad.text
    token = login(c, "tester@example.com")
    home = c.get("/")
    assert home.status_code == 200 and "Projects" in home.text
    assert c.post("/logout", data={"csrf_token": token}).status_code == 303
    assert c.get("/").status_code == 303


def test_inactive_user_cannot_login(s):
    u = make_user(s, "tester")
    u.active = False
    s.commit()
    c = client()
    r = c.post("/login", data={"email": u.email, "password": PASSWORD, "csrf_token": csrf_of(c)})
    assert r.status_code == 400


def test_csrf_required(s, as_role):
    p = make_project(s)
    c = as_role("admin")
    del c.headers["X-CSRF-Token"]
    assert c.post(f"/projects/{p.id}/sync").status_code == 403
    assert c.post(f"/projects/{p.id}/sync", data={"csrf_token": "wrong"}).status_code == 403
    assert s.query(Job).count() == 0


def test_tester_cannot_rebuild_developer_can(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    tester = as_role("tester")
    assert tester.post(f"/branches/{b.id}/rebuild").status_code == 403
    assert tester.post(f"/projects/{p.id}/sync").status_code == 403
    assert tester.get(f"/projects/{p.id}").status_code == 200
    assert tester.get("/projects/new").status_code == 403
    dev = make_user(s, "developer")
    c = client()
    c.headers["X-CSRF-Token"] = login(c, dev.email)
    assert c.post(f"/branches/{b.id}/rebuild").status_code == 303
    s.expire_all()
    build = s.query(Build).one()
    assert build.status == "queued" and build.odoo_version == "18.0"
    assert s.query(Job).filter_by(type="build").one().payload == {"build_id": build.id}


# ---------- projects ----------

PROJECT_FORM = {
    "name": "Payroll ZM", "slug": "", "repo_url": "https://github.com/acme/payroll_extension.git",
    "credential_id": "", "default_odoo_version": "18.0", "edition": "enterprise",
    "module_name": "payroll_zm", "modules_to_install": "", "auto_build_regex": "^v18$",
    "branch_exclude_regex": "",
}


def test_create_and_edit_project_with_accounts(s, as_role):
    make_account(s, token="ghp_supersecret123")
    other = make_account(s, name="Partner", token="ghp_partner999", default=False, username="odoo-partner")
    c = as_role("admin")
    r = c.post("/projects/new", data=PROJECT_FORM)
    assert r.status_code == 303, r.text
    p = s.query(Project).one()
    assert r.headers["location"] == f"/projects/{p.id}?tab=deps"
    assert s.query(Job).filter_by(type="sync").one().payload == {"project_id": p.id}
    assert p.slug == "payroll-zm" and p.edition == "enterprise" and p.module_name == "payroll_zm"
    assert p.credential_id is None          # blank = the default account
    assert len(p.webhook_secret) >= 32 and p.auto_build_regex == "^v18$"

    assert c.get(f"/projects/{p.id}/edit").headers["location"] == f"/projects/{p.id}?tab=settings"
    page = c.get(f"/projects/{p.id}?tab=settings")
    assert page.status_code == 200 and "ghp_supersecret123" not in page.text and "ghp_partner999" not in page.text
    assert "Auto-select new branches matching" in page.text and "Use the default credential (Default)" in page.text
    assert "Partner · odoo-partner" in page.text
    assert p.webhook_secret not in page.text

    r = c.post(f"/projects/{p.id}/edit", data={**PROJECT_FORM, "credential_id": str(other.id), "name": "Payroll"})
    assert r.status_code == 303
    s.expire_all()
    p = s.get(Project, p.id)
    assert p.name == "Payroll" and p.credential_id == other.id
    assert f'value="{other.id}" selected' in c.get(f"/projects/{p.id}?tab=settings").text
    bad = c.post(f"/projects/{p.id}/edit", data={**PROJECT_FORM, "credential_id": "999"})
    assert bad.status_code == 400 and "Choose one of the Git credentials" in bad.text


@pytest.mark.parametrize("field,value,error", [
    ("auto_build_regex", "([", "Invalid regex"),
    ("branch_exclude_regex", "(", "Invalid regex"),
    ("repo_url", "https://gitlab.com/a/b", "Not a GitHub"),
    ("modules_to_install", "Bad-Name", "Invalid module"),
])
def test_project_validation(s, as_role, field, value, error):
    c = as_role("admin")
    r = c.post("/projects/new", data={**PROJECT_FORM, field: value})
    assert r.status_code == 400 and error in r.text
    assert "ghp_supersecret123" not in r.text
    assert s.query(Project).count() == 0


def test_slug_change_rehosts_branches(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    c = as_role("admin")
    form = {**PROJECT_FORM, "slug": "pay", "name": "Payroll"}
    assert c.post(f"/projects/{p.id}/edit", data=form).status_code == 303
    s.expire_all()
    assert s.get(Branch, b.id).host == "pay-v18.preview.localhost"


def test_delete_project_enqueues_cleanup(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    s.add(Build(branch_id=b.id, commit_sha="a" * 40, odoo_version="18.0", status="running",
                container_id="abc", db_name="payroll_v18_aaaaaaa_b1"))
    s.commit()
    c = as_role("admin")
    assert c.post(f"/projects/{p.id}/delete", data={"confirm_slug": "wrong"}).status_code == 303
    s.expire_all()
    assert s.query(Project).count() == 1
    assert c.post(f"/projects/{p.id}/delete", data={"confirm_slug": p.slug}).status_code == 303
    s.expire_all()
    assert s.query(Project).count() == 0
    job = s.query(Job).filter_by(type="delete").one()
    assert job.payload["container_id"] == "abc" and job.payload["db_name"] == "payroll_v18_aaaaaaa_b1"
    assert job.payload["purge_log"] is True


def test_delete_project_refused_while_building(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    s.add(Build(branch_id=b.id, commit_sha="a" * 40, odoo_version="18.0", status="building"))
    s.commit()
    r = as_role("admin").post(f"/projects/{p.id}/delete", data={"confirm_slug": p.slug},
                              follow_redirects=True)
    assert "while v18 is building" in r.text
    s.expire_all()
    assert s.query(Project).count() == 1 and s.query(Job).count() == 0


def test_delete_project_admin_only(s, as_role):
    p = make_project(s)
    r = as_role("developer").post(f"/projects/{p.id}/delete", data={"confirm_slug": p.slug})
    assert r.status_code == 403


def test_project_detail_lists_branches(s, as_role):
    p = make_project(s)
    make_branch(s, p)
    make_branch(s, p, name="feature/tax", slug="feature-tax", sha="b" * 40)
    c = as_role("tester")
    r = c.get(f"/projects/{p.id}")
    assert r.status_code == 200 and "feature/tax" in r.text and "admin</code> / <code>admin" in r.text
    r = c.get(f"/projects/{p.id}/branches?q=feature")
    assert "feature/tax" in r.text and ">v18<" not in r.text


# ---------- branches ----------

def test_version_change_queues_build(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    c = as_role("developer")
    r = c.post(f"/branches/{b.id}/version", data={"odoo_version": "19.0"},
               headers={"HX-Request": "true", "HX-Target": "branches-table"})
    assert r.status_code == 200 and 'id="branches-table"' in r.text
    s.expire_all()
    assert s.get(Branch, b.id).odoo_version_override == "19.0"
    first = s.query(Build).one()
    assert first.odoo_version == "19.0" and first.status == "queued"

    c.post(f"/branches/{b.id}/version", data={"odoo_version": "18.0"})
    s.expire_all()
    assert s.get(Branch, b.id).odoo_version_override is None
    builds = {x.odoo_version: x.status for x in s.query(Build).all()}
    assert builds == {"19.0": "removed", "18.0": "queued"}


def test_disabled_version_rejected(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    c = as_role("developer")
    c.post(f"/branches/{b.id}/version", data={"odoo_version": "16.0"})
    s.expire_all()
    assert s.get(Branch, b.id).odoo_version_override is None and s.query(Build).count() == 0


def test_branch_settings_rebuild_on_change(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    c = as_role("developer")
    r = c.post(f"/branches/{b.id}/settings", data={"odoo_version_override": "", "modules_override": "payroll_zm",
                                                     "demo_data_override": "on", "enabled": "on"})
    assert r.status_code == 303
    s.expire_all()
    b = s.get(Branch, b.id)
    assert b.modules_override == "payroll_zm" and b.demo_data_override is True
    assert s.query(Build).filter_by(status="queued").count() == 1


def test_stop_and_wake(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    build = Build(branch_id=b.id, commit_sha="a" * 40, odoo_version="18.0", status="running", container_id="c1")
    s.add(build)
    s.commit()
    dev = as_role("developer")
    dev.post(f"/branches/{b.id}/stop")
    assert s.query(Job).filter_by(type="stop").one().payload == {"build_id": build.id}
    build.status = "stopped"
    s.commit()
    tester = make_user(s, "tester")
    c = client()
    c.headers["X-CSRF-Token"] = login(c, tester.email)
    assert c.post(f"/branches/{b.id}/wake").status_code == 303
    assert s.query(Job).filter_by(type="start").one().payload == {"build_id": build.id}


# ---------- friendly links ----------

def test_friendly_links(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    other = make_branch(s, p, name="feature/x", slug="feature-x", sha="c" * 40)
    s.add(Build(branch_id=b.id, commit_sha="a" * 40, odoo_version="18.0", status="running"))
    s.commit()
    c = as_role("tester")
    r = c.get("/18.0/payroll/v18")
    assert r.status_code == 302 and r.headers["location"] == "http://payroll-v18.preview.localhost"
    r = c.get("/19.0/payroll/v18")
    assert r.status_code == 404 and "runs Odoo 18.0" in r.text and "/18.0/payroll/v18" in r.text
    r = c.get("/18.0/payroll/feature/x")
    assert r.status_code == 302 and r.headers["location"] == f"/branches/{other.id}"
    assert c.get("/18.0/payroll/feature-x").headers["location"] == f"/branches/{other.id}"
    assert c.get("/18.0/payroll/nope").status_code == 404
    assert c.get("/18.0/nope/v18").status_code == 404


def test_friendly_link_requires_login(s):
    p = make_project(s)
    make_branch(s, p)
    assert client().get("/18.0/payroll/v18").headers["location"].startswith("/login")


# ---------- preview hosts (Traefik catch-all) ----------

def test_preview_host_status_page(s):
    p = make_project(s)
    b = make_branch(s, p)
    s.add(Build(branch_id=b.id, commit_sha="a" * 40, odoo_version="18.0", status="failed",
                error_summary="ERROR secret-ish detail"))
    s.commit()
    c = TestClient(app, base_url="http://payroll-v18.preview.localhost", follow_redirects=False)
    r = c.get("/web/login")
    assert r.status_code == 503 and "v18" in r.text and "The last build failed" in r.text
    assert "secret-ish" not in r.text
    assert "http://preview.localhost/branches/" in r.text
    unknown = TestClient(app, base_url="http://ghost.preview.localhost").get("/")
    assert unknown.status_code == 404 and "No preview at ghost.preview.localhost" in unknown.text
    assert c.get("/health").status_code == 200


# ---------- health & logs ----------

def test_health_reports_worker_heartbeat(s, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    c = TestClient(app, base_url="http://localhost:8000")
    body = c.get("/health").json()
    assert body["ok"] and body["db"]["ok"] and body["worker"]["ok"] is False
    (tmp_path / "worker.heartbeat").write_text(json.dumps({"ts": time.time(), "docker_ok": True, "running_jobs": 1}))
    body = c.get("/health").json()
    assert body["worker"]["ok"] and body["worker"]["docker_ok"] is True
    (tmp_path / "worker.heartbeat").write_text(json.dumps({"ts": time.time() - 120}))
    assert c.get("/health").json()["worker"]["ok"] is False


def test_build_log_tail(s, as_role, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    (tmp_path / "logs").mkdir()
    p = make_project(s)
    b = make_branch(s, p)
    build = Build(branch_id=b.id, commit_sha="a" * 40, odoo_version="18.0", status="failed")
    s.add(build)
    s.commit()
    log = tmp_path / "logs" / f"{build.id}.log"
    log.write_bytes("step 1 <ok>\nerror é\n".encode())
    c = as_role("tester")
    assert c.get(f"/builds/{build.id}").status_code == 200
    r = c.get(f"/builds/{build.id}/log?offset=0")
    assert "step 1 &lt;ok&gt;" in r.text and "error é" in r.text and 'data-done="1"' in r.text
    r = c.get(f"/builds/{build.id}/log?offset={log.stat().st_size}")
    assert "hx-swap-oob=\"beforeend:#log\"" not in r.text
    build.log_path = "/etc/passwd"
    s.commit()
    r = c.get(f"/builds/{build.id}/log/download")
    assert r.status_code == 200 and "step 1" in r.text


def test_log_poll_keeps_going_while_building(s, as_role, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    p = make_project(s)
    b = make_branch(s, p)
    build = Build(branch_id=b.id, commit_sha="a" * 40, odoo_version="18.0", status="building")
    s.add(build)
    s.commit()
    r = as_role("tester").get(f"/builds/{build.id}/log?offset=0")
    assert f'hx-get="/builds/{build.id}/log?offset=0"' in r.text


# ---------- admin ----------

def test_admin_users_and_versions(s, as_role):
    c = as_role("admin")
    me = s.query(User).filter_by(role="admin").one()
    r = c.post("/admin/users", data={"email": "New@Example.com", "name": "New", "role": "tester",
                                    "password": "longenough1"})
    assert r.status_code == 303
    assert s.query(User).filter_by(email="new@example.com").one().role == "tester"
    c.post(f"/admin/users/{me.id}", data={"role": "tester", "active": "on"})
    s.expire_all()
    assert s.get(User, me.id).role == "admin"
    assert "new@example.com" in c.get("/admin/users?q=new").text

    assert c.post("/admin/versions/18.0/pull").status_code == 303
    assert s.query(Job).filter_by(type="pull").one().payload == {"version": "18.0"}
    assert c.get("/admin/versions").status_code == 200
    assert c.get("/admin/system").status_code == 200


def test_delete_odoo_version_refused_while_used(s, as_role):
    from previewer.models import OdooVersion
    p = make_project(s)  # default_odoo_version 18.0
    c = as_role("admin")
    r = c.post("/admin/versions/18.0/delete", follow_redirects=True)
    assert "still used by project" in r.text and s.get(OdooVersion, "18.0") is not None
    p.default_odoo_version = "19.0"
    s.commit()
    assert c.post("/admin/versions/18.0/delete").status_code == 303
    s.expire_all()
    assert s.get(OdooVersion, "18.0") is None
    assert as_role("developer").post("/admin/versions/19.0/delete").status_code == 403


def test_version_demo_flags_must_match_odoo_style(s, as_role):
    from previewer.models import OdooVersion
    c = as_role("admin")
    r = c.post("/admin/versions/19.0", data={"docker_image": "odoo:19", "demo_flag_on": "",
                                             "demo_flag_off": "--without-demo=all", "enabled": "on"},
               follow_redirects=True)
    assert "needs --with-demo" in r.text
    s.expire_all()
    assert s.get(OdooVersion, "19.0").demo_flag_on == "--with-demo"
    c.post("/admin/versions", data={"version": "21.0", "docker_image": "odoo:21"})
    s.expire_all()
    v = s.get(OdooVersion, "21.0")
    assert (v.demo_flag_on, v.demo_flag_off) == ("--with-demo", "")
    s.delete(v)
    s.commit()
