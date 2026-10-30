"""Git credential choices for the dashboard forms, and the Enterprise source status shown on
the Odoo versions page and the Dependencies tab. Tokens are decrypted only to call GitHub."""
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import credentials, github
from ..models import GithubCredential, OdooVersion, Project, ProjectDependency
from ..security import decrypt

CACHE_TTL = 300
_enterprise_cache: dict[tuple, tuple[float, bool, str]] = {}


def all_accounts(s: Session) -> list[GithubCredential]:
    return list(s.scalars(select(GithubCredential).order_by(GithubCredential.name)))


def account_name(c: GithubCredential | None) -> str:
    if c is None:
        return "no account"
    return f"{c.name} · {c.username}" if c.username else c.name


def default_choice_label(s: Session) -> str:
    d = credentials.default(s)
    return (f"Use the default credential ({account_name(d)})" if d
            else "Default credential: none set (public repos only)")


def parse_choice(s: Session, value: str | None) -> tuple[int | None, str | None]:
    """Form value of an account dropdown -> (credential id or None for the fallback, error)."""
    value = (value or "").strip()
    if not value:
        return None, None
    if value.isdigit() and s.get(GithubCredential, int(value)):
        return int(value), None
    return None, "Choose one of the Git credentials"


def token_for_choice(s: Session, value: str | None, fallback: GithubCredential | None) -> str:
    cid, _ = parse_choice(s, value)
    cred = s.get(GithubCredential, cid) if cid else fallback
    return decrypt(cred.token_enc) if cred else ""


def project_account(p: Project) -> GithubCredential | None:
    return credentials.project_credential(p)


def dependency_account_desc(d: ProjectDependency) -> str:
    if d.credential:
        return account_name(d.credential)
    return f"same as project ({account_name(project_account(d.project))})"


def usage(s: Session, c: GithubCredential) -> dict:
    """Where an account is named explicitly (plus implicit default use for the default)."""
    projects = list(s.scalars(select(Project).where(Project.credential_id == c.id).order_by(Project.name)))
    deps = list(s.scalars(select(ProjectDependency).where(ProjectDependency.credential_id == c.id)))
    versions = list(s.scalars(select(OdooVersion).where(OdooVersion.enterprise_credential_id == c.id)))
    via_default = 0
    if c.is_default:
        via_default = s.scalar(select(func.count()).select_from(Project)
                               .where(Project.credential_id.is_(None))) or 0
    return {"projects": projects, "dependencies": deps, "versions": versions, "via_default": via_default,
            "explicit": len(projects) + len(deps) + len(versions)}


def enterprise_status(s: Session, version: str, fresh: bool = False) -> dict:
    """{repo_url, branch, account, ok, message} for one version's Enterprise source.
    ok is None when nothing is configured. Cached ~5 minutes unless fresh."""
    src = credentials.enterprise_source(s, version)
    if src is None:
        return {"repo_url": "", "branch": version, "account": "", "ok": None,
                "message": "No Enterprise repository set (and no ENTERPRISE_REPO_URL on the server)"}
    out = {"repo_url": src.repo_url, "branch": src.branch, "account": src.auth.label}
    key = (github.clone_url(src.repo_url), src.branch, src.auth.credential_id)
    hit = _enterprise_cache.get(key)
    if hit and not fresh and time.time() - hit[0] < CACHE_TTL:
        return {**out, "ok": hit[1], "message": hit[2]}
    name = "/".join(github.parse_repo(src.repo_url))
    try:
        ok = github.branch_exists(src.repo_url, src.auth.token, src.branch)
        msg = "" if ok else f"{name} is readable but has no {src.branch} branch"
    except github.GitHubError as e:
        ok, msg = False, f"Cannot read {name} with {src.auth.label}: {e}"
    except Exception as e:  # noqa: BLE001  (network errors)
        return {**out, "ok": False, "message": f"Could not reach GitHub: {e.__class__.__name__}"}
    _enterprise_cache[key] = (time.time(), ok, msg)
    return {**out, "ok": ok, "message": msg}


def clear_enterprise_cache() -> None:
    _enterprise_cache.clear()


def whoami(token: str) -> dict:
    """GitHub /user for a token: {login, rate_remaining, rate_limit, scopes}; raises GitHubError."""
    if not token:
        raise github.GitHubError("No token stored for this account")
    with github._client(token) as c:
        r = c.get("/user")
        if r.status_code == 401:
            raise github.GitHubError("GitHub rejected the token (401): expired or revoked?")
        if r.status_code >= 400:
            raise github.GitHubError(f"GitHub error {r.status_code}: {r.text[:200]}")
        return {
            "login": r.json().get("login", ""),
            "rate_remaining": r.headers.get("x-ratelimit-remaining"),
            "rate_limit": r.headers.get("x-ratelimit-limit"),
            "scopes": r.headers.get("x-oauth-scopes"),
        }
