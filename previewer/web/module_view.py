"""Read side of module planning for the dashboard: which modules a branch can install
(from GitHub, cached in deps) and what a given selection would install."""
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, object_session

from .. import credentials, deps, github, modules
from ..models import Branch, ModuleSet, Project

ENTERPRISE = "Odoo Enterprise"
_DEP_LINE = re.compile(r"cannot install|was not installed|not installable|Unknown module name|"
                       r"did not load|asked for but|Needs Odoo Enterprise")


@dataclass
class Available:
    branch: Branch
    version: str
    manifests: dict[str, list[str]] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    own: list[str] = field(default_factory=list)       # the project's modules (all parts for combos)
    enterprise: set[str] = field(default_factory=set)
    core: set[str] | None = None
    error: str | None = None

    @property
    def top_level(self) -> list[str]:
        return modules.top_level({m: self.manifests[m] for m in self.own})

    def rows(self) -> list[dict]:
        """One row per non-Enterprise module: source, depends, required_by, top-level flag."""
        names = [m for m in self.manifests if self.sources.get(m) != ENTERPRISE]
        required_by: dict[str, list[str]] = {}
        for m in names:
            for d in self.manifests[m]:
                required_by.setdefault(d, []).append(m)
        top = set(self.top_level)
        return [{
            "name": m, "source": self.sources.get(m, ""), "own": m in self.own,
            "depends": self.manifests[m], "required_by": sorted(required_by.get(m, [])),
            "top": m in top,
        } for m in sorted(names, key=lambda n: (n not in self.own, n))]


def branch_choices(p: Project) -> list[Branch]:
    """Branches whose modules can be listed: selected ones first (the combo has one)."""
    rows = [b for b in p.branches if not b.deleted_at]
    return sorted(rows, key=lambda b: (not b.enabled, not b.sticky, b.name))


def available(branch: Branch, version: str | None = None) -> Available:
    version = version or branch.effective_version
    project = branch.project
    s = object_session(branch)
    av = Available(branch=branch, version=version)
    try:
        if not project.is_combo:
            own = deps.repo_manifests(project.repo_url, credentials.for_project(project).token,
                                      branch.last_commit_sha or branch.name, project.root_module_name)
            av.manifests.update(own)
            av.sources.update({m: f"{project.name} repo" for m in own})
            av.own = sorted(own)
        for r in deps.resolve(branch, version):
            dep = r.dependency
            found = deps.repo_manifests(dep.repo_url, deps.dependency_token(dep), r.branch,
                                        dep.root_module_name)
            for m, d in found.items():
                av.manifests.setdefault(m, d)
                av.sources.setdefault(m, f"{dep.repo_name} @ {r.branch}")
            if project.is_combo:
                av.own += sorted(found)
        if project.edition == "enterprise":
            av.enterprise = deps.enterprise_modules(s, version) or set()
        av.core = deps.core_modules(version, credentials.for_project(project).token)
    except github.GitHubError as e:
        av.error = str(e)
    for m in av.enterprise:
        av.manifests.setdefault(m, [])
        av.sources.setdefault(m, ENTERPRISE)
    return av


def plan_for(av: Available, requested: list[str]) -> tuple[modules.InstallPlan, str]:
    """Plan + readable explanation of what cannot install (empty when all is fine)."""
    requested = requested or list(av.own)
    p = modules.plan(requested, av.manifests, av.core, av.sources)
    ent = None
    if av.branch.project.edition != "enterprise" and p.missing:
        ent = deps.enterprise_modules(object_session(av.branch), av.version)
    return p, modules.explain(p, ent, av.branch.project.edition)


def plan_from_dict(d: dict | None) -> modules.InstallPlan | None:
    """A Build.install_plan written by the worker, as an InstallPlan (None when absent)."""
    if not d:
        return None
    keys = ("requested", "install", "core", "missing", "sources", "top_level")
    return modules.InstallPlan(**{k: d.get(k) or ([] if k not in ("missing", "sources") else {})
                                  for k in keys})


def explain_dict(d: dict | None, edition: str = "community") -> str:
    p = plan_from_dict(d)
    return modules.explain(p, None, edition) if p and p.missing else ""


def grouped_install(p: modules.InstallPlan) -> list[tuple[str, list[str]]]:
    """Installed repo modules grouped by where they come from."""
    groups: dict[str, list[str]] = {}
    for m in p.install:
        groups.setdefault(p.sources.get(m) or "other", []).append(m)
    return sorted(groups.items(), key=lambda kv: (kv[0] == ENTERPRISE, kv[0]))


def parse_module_list(values: list[str], text: str) -> list[str]:
    """Checkbox values plus a free-text list, deduplicated in order."""
    extra = [m.strip() for m in (text or "").replace("\n", ",").split(",") if m.strip()]
    return list(dict.fromkeys([v.strip() for v in values if v.strip()] + extra))


def modules_source(branch: Branch) -> str:
    """Where this branch's modules list comes from, for display."""
    if branch.modules_override and branch.modules_override.strip():
        return f"custom list: {branch.modules_override}"
    if branch.module_set is not None:
        return f"module set “{branch.module_set.name}”"
    return project_default_desc(branch.project)


def project_default_desc(p: Project) -> str:
    if p.module_set is not None:
        return f"module set “{p.module_set.name}” ({p.module_set.modules})"
    if p.modules_to_install:
        return f"list: {p.modules_to_install}"
    if p.is_plain:
        return "just the base (no apps chosen)"
    return "every module of all parts" if p.is_combo else "every module in the repo"


def set_users(s: Session, ms: ModuleSet) -> list[str]:
    """Where a module set is used (project default, branches)."""
    out = []
    if ms.project.module_set_id == ms.id:
        out.append("project default")
    out += [f"branch {b.name}" for b in ms.project.branches if b.module_set_id == ms.id]
    return out


def dependency_lines(summary: str | None, limit: int = 3) -> list[str]:
    """Leading dependency explanations of an error summary (empty when it starts otherwise)."""
    lines = [ln.strip() for ln in (summary or "").splitlines() if ln.strip()]
    lead = []
    for ln in lines:
        if not _DEP_LINE.search(ln):
            break
        lead.append(ln)
    return lead[:limit]
