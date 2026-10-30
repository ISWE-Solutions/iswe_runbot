"""Which repos and branches a build needs besides the project's own branch.

Shared by the web app (setup screens, pre-build check) and the worker (pipeline), so both
always agree on what a build of a branch will fetch.
"""
import ast
import time
from dataclasses import dataclass

from sqlalchemy.orm import object_session

from . import credentials, github, modules
from .models import Branch, ProjectDependency


@dataclass
class ResolvedDep:
    dependency: ProjectDependency
    branch: str
    source: str  # "branch" (per-branch override) | "project" (version mapping) | "default"


@dataclass
class CheckResult:
    label: str      # e.g. "bank_zm" or "Odoo Enterprise"
    repo_url: str
    branch: str
    ok: bool
    message: str = ""


def resolve(branch: Branch, version: str | None = None) -> list[ResolvedDep]:
    version = version or branch.effective_version
    overrides = branch.dependency_branches or {}
    out = []
    for dep in branch.project.dependencies:
        if overrides.get(str(dep.id)):
            out.append(ResolvedDep(dep, overrides[str(dep.id)], "branch"))
        elif (dep.branch_map or {}).get(version):
            out.append(ResolvedDep(dep, dep.branch_map[version], "project"))
        else:
            out.append(ResolvedDep(dep, version, "default"))
    return out


def combo_commit(refs: list[tuple[str, str]]) -> str:
    """One 40-hex id for a combined preview's set of (repo_url, sha) heads, used as its
    Branch.last_commit_sha / Build.commit_sha; changes whenever any part moves."""
    import hashlib
    joined = "\n".join(f"{u.rstrip('/').removesuffix('.git').lower()}@{sha}" for u, sha in sorted(refs))
    return hashlib.sha1(joined.encode()).hexdigest()


def dependency_token(dep: ProjectDependency) -> str:
    return credentials.for_dependency(dep).token


_cache: dict[tuple, tuple[float, object]] = {}
CACHE_SECONDS = 300


def _cached(key: tuple, fn):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = fn()
    _cache[key] = (time.monotonic(), value)
    return value


def _parse_depends(text: str) -> list[str]:
    try:
        return list(ast.literal_eval(text).get("depends", []))
    except (ValueError, SyntaxError, AttributeError):
        return []


def repo_manifests(repo_url: str, token: str, ref: str, root_module: str) -> dict[str, list[str]]:
    """{module: depends} for a repo at ref, read from GitHub (root module or one level deep)."""
    def load():
        root = github.get_file(repo_url, token, "__manifest__.py", ref)
        if root is not None:
            return {root_module: _parse_depends(root)}
        out = {}
        for d in (github.top_level_dirs(repo_url, token, ref) or [])[:60]:
            text = github.get_file(repo_url, token, f"{d}/__manifest__.py", ref)
            if text is not None:
                out[d] = _parse_depends(text)
        return out
    return _cached(("manifests", repo_url, ref, root_module), load)


def enterprise_modules(s, version: str) -> set[str] | None:
    """Module names in the Enterprise source for the version, or None if unknown."""
    src = credentials.enterprise_source(s, version)
    if src is None:
        return None
    dirs = _cached(("enterprise", src.repo_url, src.branch),
                   lambda: github.top_level_dirs(src.repo_url, src.auth.token, src.branch))
    return set(dirs) if dirs is not None else None


def enterprise_needs(s, project, ref: str, version: str, deps: list[tuple[ProjectDependency, str]],
                     project_token: str) -> dict[str, list[str]] | None:
    """{module: [enterprise-only modules it depends on]} for the project at ref plus its
    dependencies at their branches. None when it cannot be determined (no Enterprise list
    for the version, or GitHub unreachable)."""
    try:
        ent = enterprise_modules(s, version)
        if ent is None:
            return None
        provided = dict(repo_manifests(project.repo_url, project_token, ref,
                                       project.root_module_name))
        for dep, dep_ref in deps:
            provided.update(repo_manifests(dep.repo_url, dependency_token(dep), dep_ref,
                                           dep.root_module_name))
    except github.GitHubError:
        return None
    needs = {m: sorted(d for d in depends if d in ent and d not in provided)
             for m, depends in provided.items()}
    return {m: d for m, d in needs.items() if d}


ODOO_CORE_REPO = "https://github.com/odoo/odoo.git"


def core_modules(version: str, token: str = "") -> set[str] | None:
    """Odoo Community module names for the version, from the public odoo/odoo repo."""
    def load():
        addons = github.list_dirs(ODOO_CORE_REPO, token, "addons", version)
        base = github.list_dirs(ODOO_CORE_REPO, token, "odoo/addons", version)
        return None if addons is None else set(addons) | set(base or ["base"])
    try:
        return _cached(("core", version), load)
    except github.GitHubError:
        return None


def module_plan(branch: Branch, version: str | None = None) -> modules.InstallPlan | None:
    """The install plan a build of this branch would use, read from GitHub (None when GitHub
    cannot be read). Mirrors the worker's plan, which uses the exported files."""
    version = version or branch.effective_version
    project = branch.project
    s = object_session(branch)
    try:
        manifests, sources = {}, {}
        if not project.is_combo:
            own = repo_manifests(project.repo_url, credentials.for_project(project).token,
                                 branch.last_commit_sha or branch.name, project.root_module_name)
            manifests.update(own)
            sources.update({m: f"{project.name} repo" for m in own})
        for r in resolve(branch, version):
            dep = r.dependency
            found = repo_manifests(dep.repo_url, dependency_token(dep), r.branch, dep.root_module_name)
            manifests.update(found)
            sources.update({m: f"{dep.repo_name} @ {r.branch}" for m in found})
        ent = enterprise_modules(s, version) if project.edition == "enterprise" else None
        core = core_modules(version, credentials.for_project(project).token)
    except github.GitHubError:
        return None
    for m in ent or ():
        manifests.setdefault(m, [])  # available; their own depends are core/enterprise
        sources.setdefault(m, "Odoo Enterprise")
    requested = [m.strip() for m in branch.effective_modules.split(",") if m.strip()]
    if not requested:
        requested = sorted(m for m, src in sources.items() if src != "Odoo Enterprise"
                           and (project.is_combo or src == f"{project.name} repo"))
    return modules.plan(requested, manifests, core, sources)


def describe_needs(needs: dict[str, list[str]]) -> str:
    parts = [f"{m} depends on {', '.join(d)}" for m, d in sorted(needs.items())]
    return "Needs Odoo Enterprise: " + "; ".join(parts) + ". Set the project's edition to Enterprise."


def check(branch: Branch, version: str | None = None) -> list[CheckResult]:
    """Verify on GitHub that every dependency branch (and Enterprise) exists for the version
    this branch would build. One API call per dependency."""
    version = version or branch.effective_version
    results = []
    for r in resolve(branch, version):
        dep = r.dependency
        try:
            ok = github.branch_exists(dep.repo_url, dependency_token(dep), r.branch)
            msg = "" if ok else f"{dep.repo_name} has no branch {r.branch!r}"
        except github.GitHubError as e:
            ok, msg = False, str(e)
        results.append(CheckResult(dep.root_module_name, dep.repo_url, r.branch, ok, msg))
    project = branch.project
    s = object_session(branch)
    if project.edition == "community":
        needs = enterprise_needs(s, project, branch.last_commit_sha or branch.name, version,
                                 [(r.dependency, r.branch) for r in resolve(branch, version)],
                                 credentials.for_project(project).token)
        if needs:
            results.append(CheckResult("Odoo Community", "", version, False, describe_needs(needs)))
    p = module_plan(branch, version)
    if p is not None and not p.ok:
        ent = enterprise_modules(s, version) if project.edition != "enterprise" else None
        results.append(CheckResult("Modules", "", version, False,
                                   modules.explain(p, ent, project.edition)))
    if project.edition == "enterprise":
        src = credentials.enterprise_source(s, version)
        if src is None:
            results.append(CheckResult("Odoo Enterprise", "", version, False,
                                       f"No Enterprise repository is set for Odoo {version} "
                                       "(Odoo versions page)"))
        else:
            name = "/".join(github.parse_repo(src.repo_url))
            try:
                ok = github.branch_exists(src.repo_url, src.auth.token, src.branch)
                msg = "" if ok else (f"{name} has no {src.branch} branch, or {src.auth.label} "
                                     "cannot read it")
            except github.GitHubError as e:
                ok, msg = False, f"{name} with {src.auth.label}: {e}"
            results.append(CheckResult("Odoo Enterprise", src.repo_url, src.branch, ok, msg))
    return results
