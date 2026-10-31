"""Sign in with Microsoft / GitHub. Never reaches Microsoft or GitHub: sso._post/_get are faked."""
import base64
import json
import logging
import re
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from previewer.config import get_settings
from previewer.models import User
from previewer.security import hash_password, verify_password
from previewer.web import sso
from previewer.web.main import app

TENANT = "11111111-2222-3333-4444-555555555555"
MS_CLIENT = "ms-client-id"
MS_SECRET = "ms-secret-DO-NOT-LOG"
GH_CLIENT = "gh-client-id"
GH_SECRET = "gh-secret-DO-NOT-LOG"
GH_TOKEN = "gho_access-token-DO-NOT-LOG"


def client() -> TestClient:
    return TestClient(app, base_url="http://preview.localhost", follow_redirects=False)


@pytest.fixture
def sso_settings(monkeypatch):
    c = get_settings()
    for name, value in (("microsoft_tenant_id", TENANT), ("microsoft_client_id", MS_CLIENT),
                        ("microsoft_client_secret", MS_SECRET), ("github_client_id", GH_CLIENT),
                        ("github_client_secret", GH_SECRET), ("github_signin_org", "ISWE-Solutions"),
                        ("tls_enabled", False), ("public_https", False)):
        monkeypatch.setattr(c, name, value)
    return c


@pytest.fixture
def no_sso(monkeypatch):
    c = get_settings()
    for name in ("microsoft_tenant_id", "microsoft_client_id", "microsoft_client_secret",
                 "github_client_id", "github_client_secret"):
        monkeypatch.setattr(c, name, "")
    return c


class FakeHTTP:
    """Records calls; answers from a {url: (status, json)} map."""

    def __init__(self, monkeypatch, answers: dict):
        self.answers, self.calls = answers, []
        monkeypatch.setattr(sso, "_post", self.call("POST"))
        monkeypatch.setattr(sso, "_get", self.call("GET"))

    def call(self, method):
        def _call(url, **kw):
            self.calls.append((method, url, kw))
            status, body = self.answers[url]
            return httpx.Response(status, json=body, request=httpx.Request(method, url))
        return _call


def b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def id_token(**claims) -> str:
    return f"{b64({'alg': 'RS256'})}.{b64(claims)}.signature"


def good_claims(session_nonce: str, **over) -> dict:
    claims = {"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": MS_CLIENT,
              "tid": TENANT, "nonce": session_nonce, "exp": int(time.time()) + 600,
              "email": "Alice@Example.com", "name": "Alice Banda"}
    claims.update(over)
    return claims


def ms_token_url() -> str:
    return f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"


def start(c: TestClient, provider: str, nxt: str | None = None) -> dict:
    r = c.get(f"/auth/{provider}", params={"next": nxt} if nxt else None)
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    return {"url": loc, **{k: v[0] for k, v in parse_qs(urlsplit(loc).query).items()}}


def login_error(c: TestClient) -> str:
    html = c.get("/login").text
    m = re.search(r'role="alert">([^<]+)<', html)
    return m.group(1) if m else ""


def signed_in(c: TestClient) -> bool:
    r = c.get("/login", params={"next": "/whoami-check"})
    return r.status_code == 303 and r.headers["location"] == "/whoami-check"


def microsoft_round_trip(c, monkeypatch, nxt=None, **claim_over):
    q = start(c, "microsoft", nxt)
    http = FakeHTTP(monkeypatch, {ms_token_url(): (200, {
        "access_token": "ms-access", "id_token": id_token(**good_claims(q["nonce"], **claim_over))})})
    r = c.get("/auth/microsoft/callback", params={"code": "the-code", "state": q["state"]})
    return r, http


def github_answers(member_state="active", emails=None) -> dict:
    org = "https://api.github.com/user/memberships/orgs/ISWE-Solutions"
    return {
        sso.GH_TOKEN: (200, {"access_token": GH_TOKEN, "token_type": "bearer"}),
        "https://api.github.com/user": (200, {"login": "bob-gh", "name": "Bob Phiri"}),
        "https://api.github.com/user/emails": (200, emails if emails is not None else [
            {"email": "bob@old.example", "primary": False, "verified": True},
            {"email": "Bob@Example.com", "primary": True, "verified": True}]),
        org: (200, {"state": member_state}) if member_state else (404, {"message": "Not Found"}),
    }


# ---------- login page ----------

def test_buttons_disabled_when_not_configured(no_sso):
    html = client().get("/login").text
    assert 'href="/auth/microsoft' not in html and 'href="/auth/github' not in html
    assert len(re.findall(r'aria-disabled="true"[^>]*data-sso="(microsoft|github)"', html)) == 2
    assert "Microsoft and GitHub sign-in are not set up yet." in html
    assert "or sign in with email and password" in html and 'name="password"' in html


def test_only_missing_provider_is_named(no_sso, monkeypatch):
    for name, value in (("microsoft_tenant_id", TENANT), ("microsoft_client_id", MS_CLIENT),
                        ("microsoft_client_secret", MS_SECRET)):
        monkeypatch.setattr(no_sso, name, value)
    html = client().get("/login", params={"next": "/projects"}).text
    assert 'href="/auth/microsoft?next=/projects"' in html
    assert 'href="/auth/github' not in html
    assert "GitHub sign-in is not set up yet." in html and "Microsoft and" not in html


def test_buttons_enabled_when_configured(sso_settings):
    html = client().get("/login").text
    assert 'href="/auth/microsoft"' in html and 'href="/auth/github"' in html
    assert "not set up yet" not in html
    assert MS_SECRET not in html and GH_SECRET not in html


def test_unconfigured_provider_start_goes_back_to_login(no_sso):
    c = client()
    r = c.get("/auth/github")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert login_error(c) == "GitHub sign-in is not set up yet."


# ---------- Microsoft ----------

def test_microsoft_redirect_has_state_and_nonce(sso_settings):
    q = start(client(), "microsoft")
    assert q["url"].startswith(f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize?")
    assert q["client_id"] == MS_CLIENT and q["response_type"] == "code"
    assert q["scope"] == "openid email profile"
    assert q["redirect_uri"] == "http://preview.localhost/auth/microsoft/callback"
    assert len(q["state"]) >= 20 and len(q["nonce"]) >= 20 and q["state"] != q["nonce"]
    assert MS_SECRET not in q["url"]


def test_callback_rejects_bad_state(sso_settings, monkeypatch, s):
    c = client()
    start(c, "microsoft")
    http = FakeHTTP(monkeypatch, {})
    r = c.get("/auth/microsoft/callback", params={"code": "x", "state": "forged"})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert http.calls == []
    assert "expired or was started in another window" in login_error(c)
    assert not signed_in(c) and s.scalars(select(User)).first() is None


def test_callback_without_started_sign_in_is_rejected(sso_settings, monkeypatch):
    c = client()
    http = FakeHTTP(monkeypatch, {})
    r = c.get("/auth/github/callback", params={"code": "x", "state": "anything"})
    assert r.status_code == 303 and http.calls == [] and not signed_in(c)


def test_microsoft_new_user_created_and_signed_in(sso_settings, monkeypatch, s, caplog):
    c = client()
    with caplog.at_level(logging.INFO):
        r, http = microsoft_round_trip(c, monkeypatch)
    assert r.status_code == 303 and r.headers["location"] == "/"
    method, url, kw = http.calls[0]
    assert (method, url) == ("POST", ms_token_url())
    assert kw["data"]["code"] == "the-code" and kw["data"]["grant_type"] == "authorization_code"
    assert kw["data"]["redirect_uri"] == "http://preview.localhost/auth/microsoft/callback"
    user = s.scalars(select(User)).one()
    assert (user.email, user.name, user.password_hash, user.active) == (
        "alice@example.com", "Alice Banda", "", True)
    assert signed_in(c)
    assert MS_SECRET not in caplog.text and "ms-access" not in caplog.text


def test_microsoft_preferred_username_and_existing_user_case_insensitive(sso_settings, monkeypatch, s):
    s.add(User(email="Carol@Example.com", name="Carol", password_hash=hash_password("pw-123456")))
    s.commit()
    r, _ = microsoft_round_trip(c := client(), monkeypatch, email=None,
                                preferred_username="carol@example.COM")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert len(s.scalars(select(User)).all()) == 1 and signed_in(c)


def test_microsoft_wrong_tenant_rejected(sso_settings, monkeypatch, s):
    other = "99999999-0000-0000-0000-000000000000"
    c = client()
    r, _ = microsoft_round_trip(c, monkeypatch, tid=other,
                                iss=f"https://login.microsoftonline.com/{other}/v2.0")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert login_error(c) == "That Microsoft account belongs to another organisation."
    assert not signed_in(c) and s.scalars(select(User)).first() is None


@pytest.mark.parametrize("over, message", [
    ({"iss": "https://login.microsoftonline.com/common/v2.0"}, "unexpected issuer"),
    ({"aud": "someone-else"}, "another application"),
    ({"nonce": "replayed"}, "did not match this browser session"),
    ({"exp": int(time.time()) - 5}, "has expired"),
    ({"email": None, "preferred_username": None}, "no email address"),
])
def test_microsoft_id_token_checks(sso_settings, monkeypatch, s, over, message):
    c = client()
    r, _ = microsoft_round_trip(c, monkeypatch, **over)
    assert r.headers["location"] == "/login"
    assert message in login_error(c)
    assert s.scalars(select(User)).first() is None


def test_microsoft_token_error_is_readable_and_logs_no_secret(sso_settings, monkeypatch, caplog):
    c = client()
    q = start(c, "microsoft")
    FakeHTTP(monkeypatch, {ms_token_url(): (400, {"error": "invalid_grant",
                                                  "error_description": "AADSTS70008 expired"})})
    with caplog.at_level(logging.INFO):
        r = c.get("/auth/microsoft/callback", params={"code": "c", "state": q["state"]})
    assert r.headers["location"] == "/login"
    assert login_error(c) == "Microsoft refused the sign-in (HTTP 400, invalid_grant)."
    assert MS_SECRET not in caplog.text


def test_provider_error_param(sso_settings, monkeypatch):
    c = client()
    q = start(c, "microsoft", "/projects")
    r = c.get("/auth/microsoft/callback", params={"error": "access_denied", "state": q["state"]})
    assert r.headers["location"] == "/login?next=%2Fprojects"
    assert "cancelled or failed (access_denied)" in login_error(c)


def test_disabled_user_refused(sso_settings, monkeypatch, s):
    s.add(User(email="alice@example.com", name="Alice", password_hash="", active=False))
    s.commit()
    c = client()
    r, _ = microsoft_round_trip(c, monkeypatch)
    assert r.headers["location"] == "/login"
    assert login_error(c) == "Your account is disabled."
    assert not signed_in(c)


def test_next_kept_through_round_trip(sso_settings, monkeypatch):
    c = client()
    r, _ = microsoft_round_trip(c, monkeypatch, nxt="/projects?tab=1")
    assert r.status_code == 303 and r.headers["location"] == "/projects?tab=1"


def test_next_preview_url_kept_but_foreign_dropped(sso_settings, monkeypatch):
    preview = "http://shop-main.preview.localhost/web"
    r, _ = microsoft_round_trip(client(), monkeypatch, nxt=preview)
    assert r.headers["location"] == preview
    r, _ = microsoft_round_trip(client(), monkeypatch, nxt="https://evil.example/")
    assert r.headers["location"] == "/"


def test_csrf_token_kept_after_sign_in(sso_settings, monkeypatch):
    c = client()
    c.get("/login")
    before = c.get("/login").text
    token = re.search(r'name="csrf_token" value="([^"]+)"', before).group(1)
    microsoft_round_trip(c, monkeypatch)
    r = c.post("/logout", data={"csrf_token": token})
    assert r.status_code == 303 and not signed_in(c)


# ---------- GitHub ----------

def test_github_redirect(sso_settings):
    q = start(client(), "github", "/projects")
    assert q["url"].startswith("https://github.com/login/oauth/authorize?")
    assert q["client_id"] == GH_CLIENT and q["scope"] == "read:org user:email"
    assert q["redirect_uri"] == "http://preview.localhost/auth/github/callback"
    assert len(q["state"]) >= 20 and GH_SECRET not in q["url"]


def test_github_member_signed_in_with_primary_email(sso_settings, monkeypatch, s, caplog):
    c = client()
    q = start(c, "github", "/projects")
    http = FakeHTTP(monkeypatch, github_answers())
    with caplog.at_level(logging.INFO):
        r = c.get("/auth/github/callback", params={"code": "gh-code", "state": q["state"]})
    assert r.status_code == 303 and r.headers["location"] == "/projects"
    assert [u for _, u, _ in http.calls] == [
        sso.GH_TOKEN, "https://api.github.com/user", "https://api.github.com/user/emails",
        "https://api.github.com/user/memberships/orgs/ISWE-Solutions"]
    assert http.calls[1][2]["headers"]["Authorization"] == f"Bearer {GH_TOKEN}"
    user = s.scalars(select(User)).one()
    assert (user.email, user.name, user.password_hash) == ("bob@example.com", "Bob Phiri", "")
    assert signed_in(c)
    assert GH_TOKEN not in caplog.text and GH_SECRET not in caplog.text


@pytest.mark.parametrize("member_state", [None, "pending"])
def test_github_non_member_rejected(sso_settings, monkeypatch, s, member_state, caplog):
    c = client()
    q = start(c, "github")
    FakeHTTP(monkeypatch, github_answers(member_state=member_state))
    with caplog.at_level(logging.INFO):
        r = c.get("/auth/github/callback", params={"code": "gh-code", "state": q["state"]})
    assert r.headers["location"] == "/login"
    assert login_error(c) == "Your GitHub account bob-gh is not an active member of ISWE-Solutions."
    assert not signed_in(c) and s.scalars(select(User)).first() is None
    assert GH_TOKEN not in caplog.text and GH_SECRET not in caplog.text


def test_github_without_verified_primary_email_rejected(sso_settings, monkeypatch, s):
    c = client()
    q = start(c, "github")
    FakeHTTP(monkeypatch, github_answers(emails=[
        {"email": "bob@example.com", "primary": True, "verified": False}]))
    r = c.get("/auth/github/callback", params={"code": "gh-code", "state": q["state"]})
    assert r.headers["location"] == "/login"
    assert "no verified primary email" in login_error(c)
    assert s.scalars(select(User)).first() is None


def test_github_token_exchange_error(sso_settings, monkeypatch):
    c = client()
    q = start(c, "github")
    FakeHTTP(monkeypatch, {sso.GH_TOKEN: (200, {"error": "bad_verification_code"})})
    c.get("/auth/github/callback", params={"code": "old", "state": q["state"]})
    assert login_error(c) == "GitHub refused the sign-in (HTTP 200, bad_verification_code)."


def test_state_is_single_use(sso_settings, monkeypatch):
    c = client()
    q = start(c, "github")
    http = FakeHTTP(monkeypatch, github_answers())
    c.get("/auth/github/callback", params={"code": "gh-code", "state": q["state"]})
    calls = len(http.calls)
    r = c.get("/auth/github/callback", params={"code": "gh-code", "state": q["state"]})
    assert r.headers["location"] == "/login" and len(http.calls) == calls


# ---------- HTTPS behind a proxy ----------

def test_public_https_makes_redirect_uris_https(sso_settings, monkeypatch):
    monkeypatch.setattr(sso_settings, "public_https", True)
    assert sso_settings.scheme == "https" and sso_settings.dashboard_url == "https://preview.localhost"
    assert start(client(), "microsoft")["redirect_uri"] == "https://preview.localhost/auth/microsoft/callback"
    assert start(client(), "github")["redirect_uri"] == "https://preview.localhost/auth/github/callback"


def test_public_https_marks_preview_pass_secure(sso_settings, monkeypatch, s):
    monkeypatch.setattr(sso_settings, "public_https", True)
    c = client()
    microsoft_round_trip(c, monkeypatch)
    r = c.get("/auth/preview-pass", params={"next": "https://shop-main.preview.localhost/web"})
    assert r.status_code == 303
    assert "secure" in r.headers["set-cookie"].lower()


# ---------- passwords ----------

def test_empty_password_hash_never_matches():
    assert verify_password("anything", "") is False
    assert verify_password("", "") is False
