"""Which Git credential pulls what. Projects and Enterprise use the default account unless
they name one; dependencies use the project's account unless they name one."""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from .config import get_settings
from .models import GithubCredential, OdooVersion, Project, ProjectDependency
from .security import decrypt


@dataclass
class Auth:
    token: str
    credential_id: int | None
    label: str  # for logs and screens; never contains the token


@dataclass
class EnterpriseSource:
    repo_url: str
    branch: str
    auth: Auth


def default(s: Session) -> GithubCredential | None:
    return s.scalars(select(GithubCredential).where(GithubCredential.is_default.is_(True))).first()


def _auth(cred: GithubCredential | None, fallback: str) -> Auth:
    if cred is None:
        return Auth("", None, f"no Git credential ({fallback}: public repos only)")
    who = f" ({cred.username})" if cred.username else ""
    return Auth(decrypt(cred.token_enc), cred.id, f"credential {cred.name!r}{who}")


def project_credential(project: Project) -> GithubCredential | None:
    return project.credential or default(object_session(project))


def for_project(project: Project) -> Auth:
    return _auth(project_credential(project), "no default credential")


def for_dependency(dep: ProjectDependency) -> Auth:
    return _auth(dep.credential or project_credential(dep.project), "no default credential")


def enterprise_source(s: Session, version: str) -> EnterpriseSource | None:
    """None when no Enterprise repo is configured for the version (nor ENTERPRISE_REPO_URL)."""
    v = s.get(OdooVersion, version)
    url = (v.enterprise_repo_url if v else "") or get_settings().enterprise_repo_url
    if not url:
        return None
    branch = (v.enterprise_branch if v else "") or version
    cred = (v.enterprise_credential if v else None) or default(s)
    return EnterpriseSource(url, branch, _auth(cred, "no default credential"))
