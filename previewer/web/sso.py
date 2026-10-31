"""Sign in with Microsoft (Entra ID, OIDC authorization-code flow) and GitHub (OAuth App,
members of one organisation only). httpx only. Never log or render the client secrets or
access tokens: SSOError messages are shown on the login page and written to the log."""
import base64
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from ..config import Settings, get_settings

MS_LOGIN = "https://login.microsoftonline.com"
GH_AUTHORIZE = "https://github.com/login/oauth/authorize"
GH_TOKEN = "https://github.com/login/oauth/access_token"
GH_API = "https://api.github.com"
MS_SCOPE = "openid email profile"
GH_SCOPE = "read:org user:email"
TIMEOUT = 15
LABELS = {"microsoft": "Microsoft", "github": "GitHub"}


class SSOError(Exception):
    """Readable reason a sign-in was refused; safe to show and log."""


@dataclass
class Identity:
    email: str
    name: str


def configured(provider: str, c: Settings | None = None) -> bool:
    c = c or get_settings()
    if provider == "microsoft":
        return bool(c.microsoft_tenant_id and c.microsoft_client_id and c.microsoft_client_secret)
    if provider == "github":
        return bool(c.github_client_id and c.github_client_secret and c.github_signin_org)
    return False


def not_set_up_message(c: Settings | None = None) -> str:
    """'Microsoft and GitHub sign-in are not set up yet.' naming only the missing ones."""
    missing = [LABELS[p] for p in LABELS if not configured(p, c)]
    if not missing:
        return ""
    return f"{' and '.join(missing)} sign-in {'is' if len(missing) == 1 else 'are'} not set up yet."


def redirect_uri(provider: str) -> str:
    return f"{get_settings().dashboard_url}/auth/{provider}/callback"


def new_state() -> str:
    return secrets.token_urlsafe(24)


def _post(url: str, **kw) -> httpx.Response:
    return httpx.post(url, timeout=TIMEOUT, **kw)


def _get(url: str, **kw) -> httpx.Response:
    return httpx.get(url, timeout=TIMEOUT, **kw)


def _json(r: httpx.Response, what: str):
    try:
        return r.json()
    except ValueError as e:
        raise SSOError(f"{what} returned an unreadable answer (HTTP {r.status_code}).") from e


# ---------- Microsoft ----------

def _tenant(c: Settings) -> str:
    return c.microsoft_tenant_id.strip()


def microsoft_authorize_url(state: str, nonce: str) -> str:
    c = get_settings()
    query = urlencode({"client_id": c.microsoft_client_id, "response_type": "code",
                       "redirect_uri": redirect_uri("microsoft"), "response_mode": "query",
                       "scope": MS_SCOPE, "state": state, "nonce": nonce})
    return f"{MS_LOGIN}/{_tenant(c)}/oauth2/v2.0/authorize?{query}"


def _jwt_claims(token: str) -> dict:
    """Payload of a JWT without checking the signature: allowed for an ID token received
    directly from the token endpoint over TLS (OpenID Connect Core 3.1.3.7)."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as e:
        raise SSOError("Microsoft returned an unreadable ID token.") from e
    if not isinstance(claims, dict):
        raise SSOError("Microsoft returned an unreadable ID token.")
    return claims


def check_id_token(claims: dict, nonce: str, now: float | None = None) -> Identity:
    c = get_settings()
    tenant = _tenant(c).lower()
    if str(claims.get("tid", "")).lower() != tenant:
        raise SSOError("That Microsoft account belongs to another organisation.")
    if str(claims.get("iss", "")).lower() != f"{MS_LOGIN}/{tenant}/v2.0":
        raise SSOError("The Microsoft sign-in came from an unexpected issuer.")
    aud = claims.get("aud")
    if c.microsoft_client_id not in (aud if isinstance(aud, list) else [aud]):
        raise SSOError("The Microsoft sign-in was meant for another application.")
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise SSOError("The Microsoft sign-in did not match this browser session. Please try again.")
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or exp <= (now if now is not None else time.time()):
        raise SSOError("The Microsoft sign-in has expired. Please try again.")
    email = str(claims.get("email") or claims.get("preferred_username") or "").strip().lower()
    if "@" not in email:
        raise SSOError("Your Microsoft account has no email address.")
    return Identity(email=email, name=str(claims.get("name") or "").strip())


def microsoft_identity(code: str, nonce: str) -> Identity:
    c = get_settings()
    try:
        r = _post(f"{MS_LOGIN}/{_tenant(c)}/oauth2/v2.0/token", data={
            "client_id": c.microsoft_client_id, "client_secret": c.microsoft_client_secret,
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri("microsoft"), "scope": MS_SCOPE})
    except httpx.HTTPError as e:
        raise SSOError(f"Could not reach Microsoft ({e.__class__.__name__}).") from e
    data = _json(r, "Microsoft")
    if r.status_code != 200 or not isinstance(data, dict) or not data.get("id_token"):
        err = data.get("error", "") if isinstance(data, dict) else ""
        raise SSOError(f"Microsoft refused the sign-in (HTTP {r.status_code}{', ' + err if err else ''}).")
    return check_id_token(_jwt_claims(data["id_token"]), nonce)


# ---------- GitHub ----------

def github_authorize_url(state: str) -> str:
    c = get_settings()
    query = urlencode({"client_id": c.github_client_id, "redirect_uri": redirect_uri("github"),
                       "scope": GH_SCOPE, "state": state, "allow_signup": "false"})
    return f"{GH_AUTHORIZE}?{query}"


def _api(path: str, token: str) -> httpx.Response:
    try:
        return _get(f"{GH_API}{path}", headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})
    except httpx.HTTPError as e:
        raise SSOError(f"Could not reach GitHub ({e.__class__.__name__}).") from e


def github_identity(code: str) -> Identity:
    c = get_settings()
    org = c.github_signin_org
    try:
        r = _post(GH_TOKEN, headers={"Accept": "application/json"}, data={
            "client_id": c.github_client_id, "client_secret": c.github_client_secret,
            "code": code, "redirect_uri": redirect_uri("github")})
    except httpx.HTTPError as e:
        raise SSOError(f"Could not reach GitHub ({e.__class__.__name__}).") from e
    data = _json(r, "GitHub")
    token = data.get("access_token") if isinstance(data, dict) else None
    if r.status_code != 200 or not token:
        err = data.get("error", "") if isinstance(data, dict) else ""
        raise SSOError(f"GitHub refused the sign-in (HTTP {r.status_code}{', ' + err if err else ''}).")

    r = _api("/user", token)
    user = _json(r, "GitHub")
    if r.status_code != 200 or not isinstance(user, dict) or not user.get("login"):
        raise SSOError(f"GitHub did not return your profile (HTTP {r.status_code}).")
    login = str(user["login"])

    r = _api("/user/emails", token)
    emails = _json(r, "GitHub")
    if r.status_code != 200 or not isinstance(emails, list):
        raise SSOError(f"GitHub did not return your email addresses (HTTP {r.status_code}).")
    primary = next((e for e in emails if isinstance(e, dict) and e.get("primary")
                    and e.get("verified") and e.get("email")), None)
    if primary is None:
        raise SSOError(f"Your GitHub account {login} has no verified primary email address.")

    r = _api(f"/user/memberships/orgs/{org}", token)
    membership = _json(r, "GitHub") if r.status_code == 200 else {}
    if not isinstance(membership, dict) or membership.get("state") != "active":
        raise SSOError(f"Your GitHub account {login} is not an active member of {org}.")
    return Identity(email=str(primary["email"]).strip().lower(),
                    name=str(user.get("name") or login).strip())
