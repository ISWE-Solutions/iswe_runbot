"""Read helpers shared by routes and templates."""
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import github
from ..config import get_settings
from ..models import Branch, OdooVersion, Project
from .deps import ACTIVE_STATUSES


def branch_status(branch: Branch) -> str:
    latest = branch.latest_build
    return latest.status if latest else "none"


def branch_is_active(branch: Branch) -> bool:
    return any(b.status in ACTIVE_STATUSES for b in branch.builds)


def preview_url(branch: Branch) -> str | None:
    return get_settings().preview_url(branch.host) if branch.host else None


def served_version(branch: Branch) -> str:
    """Version of the build actually serving the preview; the friendly link must use it
    (a failed build on an override version leaves the previous version running)."""
    live = branch.live_build
    return live.odoo_version if live else branch.effective_version


def friendly_path(branch: Branch) -> str:
    if branch.project.is_combo:
        return f"/{served_version(branch)}/{branch.project.slug}"
    return f"/{served_version(branch)}/{branch.project.slug}/{branch.slug}"


def detail_url(branch: Branch) -> str:
    """Where a branch is managed: its branch page, or the combined preview's page."""
    if branch.project.is_combo:
        return f"/combos/{branch.project_id}"
    return f"/branches/{branch.id}"


def github_base(repo_url: str) -> str | None:
    try:
        owner, repo = github.parse_repo(repo_url)
    except github.GitHubError:
        return None
    return f"https://github.com/{owner}/{repo}"


def error_headline(summary: str | None) -> str:
    """First non-empty line of an error summary (the full text goes in a title/pre); when it
    starts with module dependency explanations, up to three of those lines."""
    from .module_view import dependency_lines
    lead = dependency_lines(summary)
    if len(lead) > 1:
        return "\n".join(lead)
    return next((line.strip() for line in (summary or "").splitlines() if line.strip()), "")


def project_counts(project: Project) -> Counter:
    return Counter(branch_status(b) for b in project.branches if not b.deleted_at)


def version_choices(s: Session, include: str | None = None) -> list[str]:
    versions = [v.version for v in s.scalars(
        select(OdooVersion).where(OdooVersion.enabled.is_(True)).order_by(OdooVersion.version)
    )]
    if include and include not in versions:
        versions.append(include)
    return sorted(versions, key=lambda v: [int(x) for x in v.split(".")])


def unbuilt_commit(branch: Branch) -> str | None:
    """Short sha of the branch head when auto-rebuild is off and the head is newer than
    what the preview runs (or last built), so the UI can say why it is not updating."""
    if branch.auto_rebuild or not branch.last_commit_sha:
        return None
    ref = branch.live_build or next((b for b in branch.builds if b.status != "removed"), None)
    if ref and ref.commit_sha != branch.last_commit_sha:
        return branch.last_commit_sha[:7]
    return None
