"""`sync {project_id}`: mirror GitHub branches into the branches table and queue builds.
For a combined preview it refreshes the heads of its parts instead."""
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import credentials, deps, github, gitmirror, jobs
from ..config import get_settings
from ..models import COMBO_BRANCH, Branch, Build, Project, utcnow
from ..naming import preview_host, slugify


VERSION_BRANCH = re.compile(r"^(v?\d+(\.\d+)?|main|master)$")


def is_version_branch(name: str) -> bool:
    """Long-lived branches (18.0, v18, main, master) start starred on the board."""
    return bool(VERSION_BRANCH.match(name))


def branch_listed(name: str, exclude: str) -> bool:
    """Every branch is listed unless the project hides it with the exclude regex."""
    return not (exclude and re.search(exclude, name))


def auto_build(name: str, auto_build_regex: str) -> bool:
    """A NEW branch starts selected for building only when it matches auto_build_regex."""
    return bool(auto_build_regex and re.search(auto_build_regex, name))


def _host_holder(s: Session, host: str, branch: Branch) -> Branch | None:
    return s.scalars(select(Branch).where(Branch.host == host, Branch.id != branch.id)).first()


def _needs_build(s: Session, branch: Branch, is_new: bool, new_commit: bool) -> bool:
    """A new selected branch builds once; after that only a new commit rebuilds it, and only
    while auto_rebuild is on. Deleting a branch's builds does not bring them back by itself."""
    if is_new:
        return True
    if not branch.auto_rebuild:
        return False
    latest = s.scalars(select(Build).where(Build.branch_id == branch.id, Build.status != "removed")
                       .order_by(Build.id.desc()).limit(1)).first()
    if latest is None:
        return new_commit
    return latest.commit_sha != branch.last_commit_sha


class SyncError(Exception):
    pass


def sync_project(s: Session, project_id: int) -> dict:
    """Returns counts for the job log. Caller commits."""
    project = s.get(Project, project_id)
    if project is None:
        return {"error": "project not found"}
    if project.is_combo:
        return sync_combo(s, project)
    for rx in (project.auto_build_regex, project.branch_exclude_regex):
        if rx:
            re.compile(rx)  # raise a readable error for a bad filter before calling GitHub
    auth = credentials.for_project(project)
    token = auth.token
    try:
        remote = [b for b in github.list_branches(project.repo_url, token)
                  if branch_listed(b.name, project.branch_exclude_regex)]
        existing = {b.name: b for b in project.branches}
        changed = [r for r in remote if r.name not in existing or existing[r.name].last_commit_sha != r.sha]
        github.fill_commit_details(project.repo_url, token, changed)
    except github.GitHubError as e:
        raise github.GitHubError(f"{e} (using {auth.label}; check it on the Git credentials page)") from None

    domain = get_settings().domain
    counts = {"branches": len(remote), "new": 0, "selected_new": 0, "updated": 0, "deleted": 0,
              "queued": 0, "conflicts": 0}
    seen = set()
    for r in remote:
        seen.add(r.name)
        branch = existing.get(r.name)
        is_new = branch is None
        if branch is None:
            branch = Branch(project=project, name=r.name, slug=slugify(r.name) or "branch",
                            sticky=is_version_branch(r.name),
                            enabled=auto_build(r.name, project.auto_build_regex))
            s.add(branch)
            counts["new"] += 1
            counts["selected_new"] += int(branch.enabled)
        elif branch.last_commit_sha != r.sha:
            counts["updated"] += 1
        branch.deleted_at = None
        new_commit = branch.last_commit_sha != r.sha
        if new_commit:
            branch.last_commit_sha = r.sha
            branch.last_commit_message = r.message
            branch.last_commit_author = r.author
            branch.last_commit_at = r.committed_at
        s.flush()

        host = preview_host(project.slug, branch.slug, domain, combo=project.is_combo)
        holder = _host_holder(s, host, branch)
        if holder is not None:
            branch.host = None
            branch.host_conflict = (f"Preview address {host} is already used by branch "
                                    f"{holder.name!r} of project {holder.project.name!r}")
            counts["conflicts"] += 1
            continue
        branch.host, branch.host_conflict = host, None
        s.flush()

        if branch.enabled and _needs_build(s, branch, is_new, new_commit):
            jobs.queue_build(s, branch)
            counts["queued"] += 1

    for name, branch in existing.items():
        if name not in seen and branch.deleted_at is None:
            branch.deleted_at = utcnow()  # Phase 2 cleanup removes it after the grace period
            counts["deleted"] += 1
    return counts



def _set_host(s: Session, project: Project, branch: Branch, domain: str) -> bool:
    """Assign the preview host, or record the clash. False when there is a conflict."""
    host = preview_host(project.slug, branch.slug, domain, combo=project.is_combo)
    holder = _host_holder(s, host, branch)
    if holder is not None:
        branch.host = None
        branch.host_conflict = (f"Preview address {host} is already used by branch "
                                f"{holder.name!r} of project {holder.project.name!r}")
        return False
    branch.host, branch.host_conflict = host, None
    return True


def combo_heads(branch: Branch) -> list[tuple[deps.ResolvedDep, str, object]]:
    """[(resolved part, head sha, commit date or None)] for every part of a combined preview.
    Raises SyncError naming the part when a branch is missing or unreadable."""
    out = []
    for r in deps.resolve(branch):
        dep = r.dependency
        auth = credentials.for_dependency(dep)
        try:
            sha = gitmirror.remote_head(dep.repo_url, r.branch, auth.token)
        except gitmirror.GitError as e:
            why = (f"GitHub refused access with {auth.label}" if gitmirror.is_access_error(e)
                   else str(e))
            raise SyncError(f"Part {dep.root_module_name} ({gitmirror.repo_label(dep.repo_url)}): "
                            f"{why}") from None
        if sha is None:
            raise SyncError(f"Part {dep.root_module_name}: {gitmirror.repo_label(dep.repo_url)} has "
                            f"no branch {r.branch!r}")
        when = None
        try:
            info = [github.BranchInfo(r.branch, sha)]
            github.fill_commit_details(dep.repo_url, auth.token, info)
            when = info[0].committed_at
        except github.GitHubError:
            pass
        out.append((r, sha, when))
    return out


def sync_combo(s: Session, project: Project) -> dict:
    """Refresh a combined preview: its id is a hash of its parts' heads, so it rebuilds
    (with auto_rebuild on) whenever any part's branch moves."""
    branch = next((b for b in project.branches if b.name == COMBO_BRANCH), None)
    if branch is None:
        raise SyncError(f"Combined preview {project.name} has no {COMBO_BRANCH!r} branch row")
    counts = {"parts": len(project.dependencies), "updated": 0, "queued": 0, "conflicts": 0}
    if not project.dependencies and not project.is_plain:
        raise SyncError(f"Combined preview {project.name} has no parts")
    heads = combo_heads(branch) if project.dependencies else []
    sha = deps.combo_commit([(r.dependency.repo_url, h) for r, h, _ in heads])
    is_new = branch.last_commit_sha is None
    new_commit = branch.last_commit_sha != sha
    if new_commit:
        counts["updated"] = 1
        branch.last_commit_sha = sha
        branch.last_commit_message = (" · ".join(
            f"{r.dependency.root_module_name}@{r.branch} {h[:7]}" for r, h, _ in heads)[:500]
            or f"Plain Odoo {branch.effective_version} {project.edition}, no custom code")
        branch.last_commit_author = "combined"
        dates = [w for _, _, w in heads if w]
        branch.last_commit_at = max(dates) if dates else utcnow()
    branch.deleted_at = None
    s.flush()
    if not _set_host(s, project, branch, get_settings().domain):
        counts["conflicts"] = 1
        return counts
    s.flush()
    if branch.enabled and _needs_build(s, branch, is_new, new_commit):
        jobs.queue_build(s, branch)
        counts["queued"] = 1
    return counts
