"""System > Sign-in: Microsoft / GitHub provider settings saved in the `settings` table (secrets
encrypted), .env as the fallback, applied to the login page without a restart."""
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from previewer import appsettings
from previewer.config import get_settings
from previewer.models import Setting
from previewer.security import decrypt
from previewer.web import sso

from test_web_app import client, login, make_user, user_client  # noqa: F401  (fixture)

TENANT = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"
MS_CLIENT = "11111111-aaaa-bbbb-cccc-222222222222"
MS_SECRET = "Ms~Secret.Value-DO-NOT-RENDER"
GH_CLIENT = "Ov23liAbCdEf12345678"
GH_SECRET = "0123456789abcdef-gh-secret-DO-NOT-RENDER"


@pytest.fixture(autouse=True)
def blank_env(monkeypatch):
    """No provider settings in .env unless a test sets them."""
    c = get_settings()
    for name in ("microsoft_tenant_id", "microsoft_client_id", "microsoft_client_secret",
                 "github_client_id", "github_client_secret"):
        monkeypatch.setattr(c, name, "")
    monkeypatch.setattr(c, "github_signin_org", "ISWE-Solutions")
    monkeypatch.setattr(c, "tls_enabled", False)
    monkeypatch.setattr(c, "public_https", False)
    return c


def page(c: TestClient) -> str:
    r = c.get("/admin/system", params={"tab": "sign-in"})
    assert r.status_code == 200, r.text
    return r.text


def card(html: str, provider: str) -> str:
    m = re.search(rf'<form[^>]*data-signin="{provider}".*?</form>', html, re.S)
    assert m, provider
    return m.group(0)


def status(html: str, provider: str) -> str:
    return re.search(rf'data-status="{provider}">([^<]+)<', html).group(1)


def field_value(html: str, name: str) -> str:
    m = re.search(rf'name="{name}" value="([^"]*)"', html)
    return m.group(1) if m else ""


def save_ms(c, **over):
    data = {"microsoft_tenant_id": TENANT, "microsoft_client_id": MS_CLIENT,
            "microsoft_client_secret": MS_SECRET, **over}
    return c.post("/admin/system/sign-in/microsoft", data=data)


def save_gh(c, **over):
    data = {"github_client_id": GH_CLIENT, "github_client_secret": GH_SECRET,
            "github_signin_org": "ISWE-Solutions", **over}
    return c.post("/admin/system/sign-in/github", data=data)


def stored(s, key):
    s.expire_all()
    return appsettings.stored(s, key)


# ---------- the tab ----------

def test_tab_shows_both_cards_not_set_up_with_callback_urls(user_client):
    html = page(user_client)
    assert 'data-tab="sign-in"' in html
    assert status(html, "microsoft") == "Not set up" and status(html, "github") == "Not set up"
    assert 'data-redirect="microsoft">http://preview.localhost/auth/microsoft/callback<' in html
    assert 'data-redirect="github">http://preview.localhost/auth/github/callback<' in html
    assert 'action="/admin/system/sign-in/microsoft"' in html and 'name="csrf_token"' in html
    assert "a GUID, not a domain" in html
    assert 'type="password" name="microsoft_client_secret"' in html


def test_callback_urls_https_behind_proxy(user_client, blank_env, monkeypatch):
    monkeypatch.setattr(blank_env, "public_https", True)
    html = page(user_client)
    assert 'data-redirect="microsoft">https://preview.localhost/auth/microsoft/callback<' in html
    assert 'data-redirect="github">https://preview.localhost/auth/github/callback<' in html


# ---------- Microsoft ----------

def test_save_microsoft_and_show_it(user_client, s):
    r = save_ms(user_client)
    assert r.status_code == 303 and r.headers["location"] == "/admin/system?tab=sign-in"
    assert stored(s, "microsoft_tenant_id") == TENANT and stored(s, "microsoft_client_id") == MS_CLIENT
    html = page(user_client)
    assert "Microsoft sign-in saved and ready" in html
    ms = card(html, "microsoft")
    assert status(html, "microsoft") == "Ready"
    assert field_value(ms, "microsoft_tenant_id") == TENANT
    assert field_value(ms, "microsoft_client_id") == MS_CLIENT
    assert "Saved (encrypted)" in ms and "from server .env" not in ms


def test_secret_stored_encrypted_never_rendered_and_blank_keeps_it(user_client, s):
    save_ms(user_client)
    save_gh(user_client)
    enc = stored(s, "microsoft_client_secret_enc")
    assert enc and MS_SECRET not in enc and decrypt(enc) == MS_SECRET
    gh_enc = stored(s, "github_client_secret_enc")
    assert GH_SECRET not in gh_enc and decrypt(gh_enc) == GH_SECRET
    html = page(user_client)
    assert MS_SECRET not in html and GH_SECRET not in html and enc not in html
    assert field_value(html, "microsoft_client_secret") == ""

    r = save_ms(user_client, microsoft_client_secret="", microsoft_client_id=MS_CLIENT.upper())
    assert r.status_code == 303
    assert stored(s, "microsoft_client_secret_enc") == enc
    assert stored(s, "microsoft_client_id") == MS_CLIENT.upper()
    assert sso.effective(s).microsoft_client_secret == MS_SECRET


def test_secret_never_in_repr():
    v = sso.SignInSettings(microsoft_client_secret=MS_SECRET, github_client_secret=GH_SECRET)
    assert MS_SECRET not in repr(v) and GH_SECRET not in repr(v)


def test_remove_clears_secret(user_client, s):
    save_ms(user_client)
    r = save_ms(user_client, microsoft_client_secret="", microsoft_client_secret_clear="on")
    assert r.status_code == 303
    assert stored(s, "microsoft_client_secret_enc") is None
    html = page(user_client)
    assert status(html, "microsoft") == "Not set up"
    assert "Saved (encrypted)" not in card(html, "microsoft")
    assert "Still needed: Client secret" in html


@pytest.mark.parametrize("tenant", ["iswe.co.zm", "iswesolutions.onmicrosoft.com", "not-a-guid",
                                    TENANT + "0"])
def test_invalid_tenant_refused(user_client, s, tenant):
    r = save_ms(user_client, microsoft_tenant_id=tenant)
    assert r.status_code == 400
    assert "the Directory (tenant) ID, a GUID, not a domain" in r.text
    assert "Microsoft sign-in not saved" in r.text
    assert f'value="{tenant}"' in r.text           # what was typed stays in the field
    assert MS_SECRET not in r.text
    s.expire_all()
    assert s.get(Setting, "microsoft_tenant_id") is None
    assert stored(s, "microsoft_client_secret_enc") is None and stored(s, "microsoft_client_id") is None


def test_save_needs_csrf(s):
    c = client()
    login(c, make_user(s).email)
    c.headers["X-CSRF-Token"] = "wrong"
    r = save_ms(c)
    assert r.status_code in (400, 403)
    assert stored(s, "microsoft_tenant_id") is None


def test_unknown_provider_404(user_client):
    assert user_client.post("/admin/system/sign-in/google", data={}).status_code == 404


# ---------- .env fallback ----------

def test_env_values_shown_as_from_server_env(user_client, blank_env, monkeypatch):
    for name, value in (("microsoft_tenant_id", TENANT), ("microsoft_client_id", MS_CLIENT),
                        ("microsoft_client_secret", MS_SECRET)):
        monkeypatch.setattr(blank_env, name, value)
    html = page(user_client)
    ms = card(html, "microsoft")
    assert status(html, "microsoft") == "Ready"
    assert field_value(ms, "microsoft_tenant_id") == TENANT
    assert ms.count("from server .env") == 3          # tenant, client id, secret
    assert MS_SECRET not in html and "Saved (encrypted)" not in ms


def test_db_values_override_env(user_client, blank_env, monkeypatch, s):
    for name, value in (("microsoft_tenant_id", "99999999-0000-0000-0000-000000000000"),
                        ("microsoft_client_id", "env-client"), ("microsoft_client_secret", "env-secret"),
                        ("github_signin_org", "Env-Org")):
        monkeypatch.setattr(blank_env, name, value)
    save_ms(user_client)
    save_gh(user_client, github_signin_org="Other-Org")
    v = sso.effective(s)
    assert (v.microsoft_tenant_id, v.microsoft_client_id, v.microsoft_client_secret) == (
        TENANT, MS_CLIENT, MS_SECRET)
    assert v.github_signin_org == "Other-Org"
    assert "from server .env" not in card(page(user_client), "microsoft")


def test_blank_or_env_value_falls_back_to_env(user_client, blank_env, monkeypatch, s):
    monkeypatch.setattr(blank_env, "microsoft_client_id", MS_CLIENT)
    save_ms(user_client, microsoft_client_id="")
    assert stored(s, "microsoft_client_id") is None
    save_ms(user_client, microsoft_client_id=MS_CLIENT)  # the .env value itself: nothing to save
    assert stored(s, "microsoft_client_id") is None
    assert sso.effective(s).microsoft_client_id == MS_CLIENT


def test_remove_falls_back_to_env_secret(user_client, blank_env, monkeypatch, s):
    monkeypatch.setattr(blank_env, "microsoft_client_secret", "env-secret")
    save_ms(user_client)
    assert sso.effective(s).microsoft_client_secret == MS_SECRET
    save_ms(user_client, microsoft_client_secret="", microsoft_client_secret_clear="on")
    s.expire_all()
    assert sso.effective(s).microsoft_client_secret == "env-secret"


# ---------- the login page follows at once ----------

def test_after_saving_login_buttons_enable_and_redirect_uses_saved_client(user_client):
    html = client().get("/login").text
    assert 'href="/auth/microsoft"' not in html and "are not set up yet" in html

    save_ms(user_client)
    html = client().get("/login").text
    assert 'href="/auth/microsoft"' in html and 'href="/auth/github"' not in html
    assert "GitHub sign-in is not set up yet." in html

    save_gh(user_client)
    html = client().get("/login").text
    assert 'href="/auth/microsoft"' in html and 'href="/auth/github"' in html
    assert "not set up yet" not in html
    assert MS_SECRET not in html and GH_SECRET not in html

    r = client().get("/auth/microsoft")
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith(f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize?")
    q = {k: v[0] for k, v in parse_qs(urlsplit(loc).query).items()}
    assert q["client_id"] == MS_CLIENT
    assert q["redirect_uri"] == "http://preview.localhost/auth/microsoft/callback"
    assert MS_SECRET not in loc

    r = client().get("/auth/github")
    q = {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}
    assert q["client_id"] == GH_CLIENT and GH_SECRET not in r.headers["location"]


def test_saved_secret_used_in_token_exchange(user_client, monkeypatch):
    from test_web_sso import FakeHTTP, good_claims, id_token
    save_ms(user_client)
    c = client()
    r = c.get("/auth/microsoft")
    q = {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}
    token_url = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    claims = good_claims(q["nonce"], iss=f"https://login.microsoftonline.com/{TENANT}/v2.0",
                         tid=TENANT, aud=MS_CLIENT)
    http = FakeHTTP(monkeypatch, {token_url: (200, {"id_token": id_token(**claims)})})
    r = c.get("/auth/microsoft/callback", params={"code": "the-code", "state": q["state"]})
    assert r.status_code == 303 and r.headers["location"] == "/"
    data = http.calls[0][2]["data"]
    assert data["client_id"] == MS_CLIENT and data["client_secret"] == MS_SECRET


# ---------- GitHub ----------

def test_save_github_and_show_it(user_client, s):
    r = save_gh(user_client, github_signin_org="ISWE-Labs")
    assert r.status_code == 303 and r.headers["location"] == "/admin/system?tab=sign-in"
    html = page(user_client)
    gh = card(html, "github")
    assert status(html, "github") == "Ready" and "GitHub sign-in saved and ready" in html
    assert field_value(gh, "github_client_id") == GH_CLIENT
    assert field_value(gh, "github_signin_org") == "ISWE-Labs"
    assert "Saved (encrypted)" in gh
    assert stored(s, "github_signin_org") == "ISWE-Labs"


def test_github_org_default(user_client, s):
    gh = card(page(user_client), "github")
    assert field_value(gh, "github_signin_org") == "ISWE-Solutions"
    assert 'data-source="default"' in gh
    save_gh(user_client, github_signin_org="")
    assert stored(s, "github_signin_org") is None
    assert sso.effective(s).github_signin_org == "ISWE-Solutions"


def test_github_saved_without_secret_is_not_ready(user_client):
    r = save_gh(user_client, github_client_secret="")
    assert r.status_code == 303
    html = page(user_client)
    assert "Still needed: Client secret" in html and status(html, "github") == "Not set up"


@pytest.mark.parametrize("over, message", [
    ({"github_signin_org": "../evil"}, "A GitHub organisation name"),
    ({"github_client_id": "has space"}, "Client ID"),
    ({"github_client_secret": "two words"}, "no spaces"),
])
def test_github_invalid_refused(user_client, s, over, message):
    r = save_gh(user_client, **over)
    assert r.status_code == 400 and message in r.text
    assert stored(s, "github_client_id") is None and stored(s, "github_client_secret_enc") is None
