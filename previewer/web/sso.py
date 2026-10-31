"""Sign in with Microsoft (Entra ID, OIDC authorization-code flow) and GitHub (OAuth App,
members of one organisation only). httpx only. Never log or render the client secrets or
access tokens: SSOError messages are shown on the login page and written to the log.

Provider settings are read per request (`effective`): the System > Sign-in values saved in the
`settings` table, else .env, so a save applies without a restart."""
import base64
import json
import logging
import secrets
import time
from contextlib import closing
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from .. import appsettings
from ..config import get_settings
from ..db import SessionLocal
from ..security import decrypt

MS_LOGIN = "https://login.microsoftonline.com"
GH_AUTHORIZE = "https://github.com/login/oauth/authorize"
GH_TOKEN = "https://github.com/login/oauth/access_token"
GH_API = "https://api.github.com"
MS_SCOPE = "openid email profile"
GH_SCOPE = "read:org user:email"
TIMEOUT = 15
LABELS = {"microsoft": "Microsoft", "github": "GitHub"}
log = logging.getLogger(__name__)


class SSOError(Exception):
    """Readable reason a sign-in was refused; safe to show and log."""


@dataclass
class Identity:
    email: str
    name: str


@dataclass(frozen=True)
class SignInSettings:
    """The provider settings in force. Holds decrypted secrets: keep it local to one request,
    never log or render it (repr hides the secrets)."""
    microsoft_tenant_id: str = ""
    microsoft_client_id: str = ""
    microsoft_client_secret: str = field(default="", repr=False)
    github_client_id: str = ""
    github_client_secret: str = field(default="", repr=False)
    github_signin_org: str = ""


def _secret(s: Session, provider: str, env_value: str) -> str:
    enc = appsettings.stored(s, f"{provider}_client_secret_enc")
    if not enc:
        return env_value
    try:
        return decrypt(enc)
    except RuntimeError:
        log.warning("%s sign-in: the saved client secret cannot be decrypted (ENCRYPTION_KEY "
                    "changed?); save it again on System > Sign-in", LABELS[provider])
        return ""


def effective(s: Session | None = None) -> SignInSettings:
    """System > Sign-in values (the `settings` table) where saved, else .env."""
    if s is None:
        with closing(SessionLocal()) as own:
            return effective(own)
    c = get_settings()

    def value(key: str) -> str:
        return (appsettings.stored(s, key) or "").strip() or (getattr(c, key) or "").strip()

    return SignInSettings(
        microsoft_tenant_id=value("microsoft_tenant_id"),
        microsoft_client_id=value("microsoft_client_id"),
        microsoft_client_secret=_secret(s, "microsoft", c.microsoft_client_secret),
        github_client_id=value("github_client_id"),
        github_client_secret=_secret(s, "github", c.github_client_secret),
        github_signin_org=value("github_signin_org"))


def missing(provider: str, v: SignInSettings) -> list[str]:
    """Labels of the settings a provider still needs (empty = ready)."""
    if provider == "microsoft":
        need = (("Tenant ID", v.microsoft_tenant_id), ("Client ID", v.microsoft_client_id),
                ("Client secret", v.microsoft_client_secret))
    elif provider == "github":
        need = (("Client ID", v.github_client_id), ("Client secret", v.github_client_secret),
                ("Organisation", v.github_signin_org))
    else:
        return ["provider"]
    return [label for label, val in need if not val]


def configured(provider: str, v: SignInSettings | None = None) -> bool:
    return not missing(provider, v or effective())


def not_set_up_message(v: SignInSettings | None = None) -> str:
    """'Microsoft and GitHub sign-in are not set up yet.' naming only the missing ones."""
    v = v or effective()
    names = [LABELS[p] for p in LABELS if not configured(p, v)]
    if not names:
        return ""
    return f"{' and '.join(names)} sign-in {'is' if len(names) == 1 else 'are'} not set up yet."


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

def microsoft_authorize_url(state: str, nonce: str, v: SignInSettings | None = None) -> str:
    v = v or effective()
    query = urlencode({"client_id": v.microsoft_client_id, "response_type": "code",
                       "redirect_uri": redirect_uri("microsoft"), "response_mode": "query",
                       "scope": MS_SCOPE, "state": state, "nonce": nonce})
    return f"{MS_LOGIN}/{v.microsoft_tenant_id}/oauth2/v2.0/authorize?{query}"


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


def check_id_token(claims: dict, nonce: str, now: float | None = None,
                   v: SignInSettings | None = None) -> Identity:
    v = v or effective()
    tenant = v.microsoft_tenant_id.lower()
    if str(claims.get("tid", "")).lower() != tenant:
        raise SSOError("That Microsoft account belongs to another organisation.")
    if str(claims.get("iss", "")).lower() != f"{MS_LOGIN}/{tenant}/v2.0":
        raise SSOError("The Microsoft sign-in came from an unexpected issuer.")
    aud = claims.get("aud")
    if v.microsoft_client_id not in (aud if isinstance(aud, list) else [aud]):
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


def microsoft_identity(code: str, nonce: str, v: SignInSettings | None = None) -> Identity:
    v = v or effective()
    try:
        r = _post(f"{MS_LOGIN}/{v.microsoft_tenant_id}/oauth2/v2.0/token", data={
            "client_id": v.microsoft_client_id, "client_secret": v.microsoft_client_secret,
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri("microsoft"), "scope": MS_SCOPE})
    except httpx.HTTPError as e:
        raise SSOError(f"Could not reach Microsoft ({e.__class__.__name__}).") from e
    data = _json(r, "Microsoft")
    if r.status_code != 200 or not isinstance(data, dict) or not data.get("id_token"):
        err = data.get("error", "") if isinstance(data, dict) else ""
        raise SSOError(f"Microsoft refused the sign-in (HTTP {r.status_code}{', ' + err if err else ''}).")
    return check_id_token(_jwt_claims(data["id_token"]), nonce, v=v)


# ---------- GitHub ----------

def github_authorize_url(state: str, v: SignInSettings | None = None) -> str:
    v = v or effective()
    query = urlencode({"client_id": v.github_client_id, "redirect_uri": redirect_uri("github"),
                       "scope": GH_SCOPE, "state": state, "allow_signup": "false"})
    return f"{GH_AUTHORIZE}?{query}"


def _api(path: str, token: str) -> httpx.Response:
    try:
        return _get(f"{GH_API}{path}", headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})
    except httpx.HTTPError as e:
        raise SSOError(f"Could not reach GitHub ({e.__class__.__name__}).") from e


def github_identity(code: str, v: SignInSettings | None = None) -> Identity:
    v = v or effective()
    org = v.github_signin_org
    try:
        r = _post(GH_TOKEN, headers={"Accept": "application/json"}, data={
            "client_id": v.github_client_id, "client_secret": v.github_client_secret,
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
