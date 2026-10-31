"""Phase 2 dashboard: GitHub webhook, forward-auth on previews, wake-on-visit, System page."""
import hashlib
import hmac
import json

import pytest

from previewer import appsettings
from previewer.config import get_settings
from previewer.models import COMBO_BRANCH, Branch, Build, Job, Project, ProjectDependency
from previewer.security import decrypt
from previewer.web.routes import forward_auth

from test_web_app import PASSWORD, client, csrf_of, login, make_branch, make_project, make_user, user_client  # noqa: F401

REPO = "https://github.com/acme/payroll_extension.git"  # make_project's repo (without .git)


@pytest.fixture(autouse=True)
def fresh_auth_cache():
    forward_auth.clear_cache()
    yield
    forward_auth.clear_cache()


def signed(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post_hook(event: str, payload: dict, secret: str | None):
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    if secret is not None:
        headers["X-Hub-Signature-256"] = signed(body, secret)
    return client().post("/api/webhooks/github", content=body, headers=headers)


def repo_payload(url=REPO):
    return {"ref": "refs/heads/v18", "repository": {"full_name": "acme/payroll_extension",
                                                    "clone_url": url, "html_url": url.removesuffix(".git")}}


def sync_jobs(s):
    s.expire_all()
    return [j for j in s.query(Job).all() if j.type == "sync"]


# ---------- webhook ----------

def test_webhook_push_enqueues_one_sync(s):
    p = make_project(s, webhook_secret="s3cret")
    r = post_hook("push", repo_payload(), "s3cret")
    assert r.status_code == 200 and r.json()["synced"] == [p.slug]
    r = post_hook("push", repo_payload(), "s3cret")
    assert r.status_code == 200 and r.json()["already_queued"] == [p.slug]
    assert [j.payload for j in sync_jobs(s)] == [{"project_id": p.id}]


def test_webhook_signature_bad_or_missing(s):
    make_project(s, webhook_secret="s3cret")
    assert post_hook("push", repo_payload(), "wrong").status_code == 401
    assert post_hook("push", repo_payload(), None).status_code == 401
    assert sync_jobs(s) == []


def test_webhook_ping_unknown_repo_and_ignored_events(s):
    make_project(s, webhook_secret="s3cret")
    r = post_hook("ping", repo_payload(), "s3cret")
    assert r.status_code == 200 and r.json()["message"] == "pong"
    assert post_hook("push", repo_payload("https://github.com/acme/other.git"), "s3cret").status_code == 404
    assert post_hook("issues", repo_payload(), "s3cret").status_code == 202
    assert sync_jobs(s) == []


def test_webhook_matches_combined_previews_by_part(s):
    combo = Project(kind="combo", name="HR suite", slug="hr-suite", repo_url="", default_odoo_version="18.0",
                    webhook_secret="combo-secret")
    s.add(combo)
    s.flush()
    s.add(ProjectDependency(project=combo, repo_url=REPO, module_name="payroll_zm"))
    s.commit()
    r = post_hook("push", repo_payload(REPO.removesuffix(".git")), "combo-secret")
    assert r.status_code == 200 and r.json()["synced"] == ["hr-suite"]


def test_webhook_needs_no_csrf_or_login(s):
    make_project(s, webhook_secret="s3cret")
    assert post_hook("push", repo_payload(), "s3cret").status_code == 200


# ---------- forward-auth ----------

def fa(c, host="payroll-v18.preview.localhost", uri="/web/login?db=x"):
    return c.get("/auth/forward", headers={"X-Forwarded-Host": host, "X-Forwarded-Proto": "http",
                                           "X-Forwarded-Uri": uri})


def get_pass(c, url="http://payroll-v18.preview.localhost/web/login?db=x"):
    r = c.get("/auth/preview-pass", params={"next": url})
    assert r.status_code == 303 and r.headers["location"] == url
    cookie = r.headers["set-cookie"]
    assert "previewer_pass=" in cookie and "Domain=preview.localhost" in cookie and "HttpOnly" in cookie
    return r


def test_preview_pass_needs_login_and_a_preview_url(s):
    c = client()
    r = c.get("/auth/preview-pass", params={"next": "http://payroll-v18.preview.localhost/x"})
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=%2Fauth%2Fpreview-pass")
    assert c.get("/auth/preview-pass", params={"next": "https://evil.example/x"}).headers["location"] == "/"
    u = make_user(s)
    login(c, u.email)
    assert c.get("/auth/preview-pass", params={"next": "http://preview.localhost.evil.com/"}).headers["location"] == "/"


def test_dashboard_session_cookie_is_host_only(s):
    u = make_user(s)
    c = client()
    token = csrf_of(c)
    r = c.post("/login", data={"email": u.email, "password": PASSWORD, "csrf_token": token, "next": "/"})
    session = [h for h in r.headers.get_list("set-cookie") if h.startswith("previewer_session=")][0]
    assert "domain=" not in session.lower()


def test_forward_auth_redirects_anonymous_to_login_with_next(s):
    p = make_project(s)
    make_branch(s, p)
    r = fa(client())
    assert r.status_code == 302
    assert r.headers["location"] == (f"{get_settings().dashboard_url}/auth/preview-pass?next="
                                     "http%3A%2F%2Fpayroll-v18.preview.localhost%2Fweb%2Flogin%3Fdb%3Dx")


def test_forward_auth_allows_logged_in_user_and_public_demo(s):
    p = make_project(s)
    make_branch(s, p)
    u = make_user(s)
    c = client()
    login(c, u.email)
    assert fa(c).status_code == 302  # dashboard session alone is not enough: needs a pass
    get_pass(c)
    assert fa(c).status_code == 204
    p.preview_auth_required = False
    s.commit()
    forward_auth.clear_cache()
    assert fa(client()).status_code == 204


def test_forward_auth_unknown_host_and_inactive_user(s):
    assert fa(client(), host="nothing.preview.localhost").status_code == 204
    p = make_project(s)
    make_branch(s, p)
    u = make_user(s)
    c = client()
    login(c, u.email)
    get_pass(c)
    u.active = False
    s.commit()
    forward_auth.clear_cache()
    assert fa(c).status_code == 302


def test_login_redirects_back_to_preview_but_not_elsewhere(s):
    u = make_user(s)
    c = client()
    page = c.get("/login?next=http%3A%2F%2Fpayroll-v18.preview.localhost%2Fweb")
    assert 'value="http://payroll-v18.preview.localhost/web"' in page.text
    token = __import__("re").search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    r = c.post("/login", data={"email": u.email, "password": PASSWORD, "csrf_token": token,
                               "next": "http://payroll-v18.preview.localhost/web"})
    assert r.status_code == 303 and r.headers["location"] == "http://payroll-v18.preview.localhost/web"
    for evil in ("https://evil.example.com/", "//evil.example.com", "http://preview.localhost.evil.com/",
                 "javascript:alert(1)"):
        assert c.get("/login", params={"next": evil}).headers["location"] == "/"
    # already logged in + safe next: straight back
    assert c.get("/login?next=/projects").headers["location"] == "/projects"


# ---------- wake on visit ----------

def sleeping_branch(s, status="sleeping", stale=False):
    from previewer.models import utcnow
    p = make_project(s)
    b = make_branch(s, p)
    build = Build(branch_id=b.id, commit_sha="a" * 40, odoo_version="18.0", status=status,
                  host=b.host, db_name="d1")
    s.add(build)
    if stale:
        b.stale_since = utcnow()
    s.commit()
    return b, build


def visit(c, host="payroll-v18.preview.localhost"):
    return c.get("/web/login", headers={"Host": host})


def test_visit_wakes_sleeping_preview_once(s):
    b, build = sleeping_branch(s)
    r = visit(client())
    assert r.status_code == 503 and "Waking up" in r.text and r.headers["retry-after"] == "5"
    visit(client())
    s.expire_all()
    wakes = [j for j in s.query(Job).all() if j.type == "wake"]
    assert len(wakes) == 1 and wakes[0].payload == {"build_id": build.id} and wakes[0].build_id == build.id


def test_stale_preview_wakes_only_for_signed_in_users(s):
    b, build = sleeping_branch(s, status="stopped", stale=True)
    r = visit(client())
    assert r.status_code == 503 and "Sign in" in r.text
    assert not [j for j in s.query(Job).all() if j.type == "wake"]
    u = make_user(s)
    c = client()
    login(c, u.email)
    r = visit(c)
    assert "Waking up" in r.text and "stale" in r.text
    s.expire_all()
    assert len([j for j in s.query(Job).all() if j.type == "wake"]) == 1


def test_stopped_but_not_stale_keeps_status_page(s):
    sleeping_branch(s, status="stopped")
    r = visit(client())
    assert "Waking up" not in r.text and not [j for j in s.query(Job).all() if j.type == "wake"]


# ---------- System page ----------

@pytest.fixture
def heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)

    def write(**data):
        import time
        (tmp_path / "worker.heartbeat").write_text(json.dumps({"ts": time.time(), "docker_ok": True, **data}))
    return write


def test_system_page_without_and_with_heartbeat_details(s, user_client, heartbeat):
    c = user_client
    r = c.get("/admin/system")
    assert r.status_code == 200 and "Not responding" in r.text
    heartbeat(disk={"free_gb": 3.1, "total_gb": 228, "percent_used": 98.6},
              docker={"cpus": 10, "mem_total_gb": 7.7},
              previews=[{"build_id": 7, "name": "bank / v19", "mem_mb": 120, "cpu_percent": 0.4},
                        {"build_id": 8, "name": "hr-suite"}])
    r = c.get("/admin/system")
    assert "Running" in r.text and "99%" in r.text and "Disk 99% full" in r.text and "bank / v19" in r.text
    assert "120 MB" in r.text
    board = c.get("/")
    assert "Disk 99% full" in board.text


def test_system_settings_save_validate_and_slack_encrypted(s, user_client):
    c = user_client
    form = {k: appsettings.get(s, k) for k in appsettings.defaults() if k != "slack_webhook_url_enc"}
    form.update({"max_running_previews": "x", "preview_memory_limit": "lots", "disk_warning_percent": "150"})
    r = c.post("/admin/system/settings", data=form)
    assert r.status_code == 400 and "Whole number" in r.text and "Like 1536m" in r.text and "Between 1 and 100" in r.text
    form.update({"max_running_previews": "10", "preview_memory_limit": "2g", "disk_warning_percent": "85",
                 "use_db_templates": "", "slack_webhook_url": "https://hooks.slack.com/services/T/B/x"})
    assert c.post("/admin/system/settings", data=form).status_code == 303
    s.expire_all()
    assert appsettings.get(s, "max_running_previews") == "10" and appsettings.get(s, "use_db_templates") == "0"
    enc = appsettings.get(s, "slack_webhook_url_enc")
    assert enc and "hooks.slack.com" not in enc and decrypt(enc).endswith("/T/B/x")
    assert "hooks.slack.com/services/T/B/x" not in c.get("/admin/system").text


def test_slack_test_message(s, user_client, monkeypatch):
    import httpx
    c = user_client
    assert "No Slack webhook" in c.post("/admin/system/slack-test").text
    from previewer.security import encrypt
    appsettings.put(s, "slack_webhook_url_enc", encrypt("https://hooks.slack.com/services/T/B/x"))
    s.commit()
    sent = {}
    monkeypatch.setattr(httpx, "post", lambda url, json, timeout: sent.update(url=url, json=json)
                        or httpx.Response(200, text="ok"))
    assert "Sent" in c.post("/admin/system/slack-test").text and sent["url"].endswith("/T/B/x")


# ---------- project access & webhook panel ----------

def test_access_switches_and_webhook_secret(s, user_client):
    p = make_project(s, webhook_secret="old-secret")
    c = user_client
    page = c.get(f"/projects/{p.id}?tab=settings").text
    assert "/api/webhooks/github" in page and "old-secret" not in page
    assert "old-secret" in c.get(f"/projects/{p.id}/webhook-secret").text
    assert c.post(f"/projects/{p.id}/access", data={"post_commit_status": "on"}).status_code == 303
    s.expire_all()
    assert not p.preview_auth_required and p.post_commit_status
    c.post(f"/projects/{p.id}/webhook-secret")
    s.expire_all()
    assert p.webhook_secret and p.webhook_secret != "old-secret"


def test_board_shows_public_and_stale_markers(s, user_client):
    from previewer.models import utcnow
    p = make_project(s, preview_auth_required=False)
    b = make_branch(s, p)
    b.stale_since = utcnow()
    s.commit()
    page = user_client.get("/?project=all").text
    assert "bi-globe2" in page and "⏸ stale" in page
