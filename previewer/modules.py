"""Install planning: which modules a build installs, which dependencies are missing and why.

Pure functions over manifests ({module: [depends]}), shared by the worker (local files) and
the web app (manifests read from GitHub), so the dashboard can show the same plan the build
will use.
"""
import re
from dataclasses import dataclass, field


@dataclass
class InstallPlan:
    requested: list[str]                       # what was asked for (or auto-detected)
    install: list[str]                         # requested + every repo module they pull in
    core: list[str]                            # Odoo core / Enterprise modules pulled in
    missing: dict[str, list[str]] = field(default_factory=dict)  # module -> missing deps
    sources: dict[str, str] = field(default_factory=dict)       # module -> where it comes from
    top_level: list[str] = field(default_factory=list)          # repo modules nothing depends on

    @property
    def ok(self) -> bool:
        return not self.missing

    def as_dict(self) -> dict:
        return {"requested": self.requested, "install": self.install, "core": self.core,
                "missing": self.missing, "sources": self.sources, "top_level": self.top_level}


def top_level(manifests: dict[str, list[str]]) -> list[str]:
    """Modules no other module in the set depends on. Installing these installs them all."""
    needed = {d for deps in manifests.values() for d in deps}
    return sorted(m for m in manifests if m not in needed)


def plan(requested: list[str], manifests: dict[str, list[str]], core: set[str] | None,
         sources: dict[str, str] | None = None) -> InstallPlan:
    """Walk the dependencies of `requested`. `manifests` = every module available from the
    project and its dependencies (incl. Enterprise if used); `core` = Odoo core module names
    (None = unknown: then anything not in `manifests` is assumed to be core)."""
    sources = sources or {}
    install, core_used, missing, seen = [], set(), {}, set()

    def visit(m: str, parent: str | None):
        if m in seen:
            return
        seen.add(m)
        if m in manifests:
            install.append(m)
            for d in manifests[m]:
                visit(d, m)
        elif core is None or m in core or m == "base":
            core_used.add(m)
        else:
            missing.setdefault(parent or m, []).append(m)

    for m in requested:
        visit(m, None)
    repo_install = [m for m in install if m not in core_used]
    return InstallPlan(
        requested=list(requested), install=repo_install, core=sorted(core_used),
        missing={k: sorted(set(v)) for k, v in missing.items()},
        sources={m: sources[m] for m in repo_install if m in sources},
        top_level=top_level(manifests_from(manifests, sources, exclude_enterprise=True)),
    )


def manifests_from(manifests: dict[str, list[str]], sources: dict[str, str],
                   exclude_enterprise: bool) -> dict[str, list[str]]:
    if not exclude_enterprise:
        return manifests
    return {m: d for m, d in manifests.items() if sources.get(m) != "Odoo Enterprise"}


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def explain(p: InstallPlan, enterprise_modules: set[str] | None = None,
            edition: str = "community") -> str:
    """One readable sentence per module that cannot install."""
    lines = []
    for module, deps in sorted(p.missing.items()):
        if deps == [module]:
            lines.append(f"{module}: {module} was asked for but is not in any repo of this build")
            continue
        ent = [d for d in deps if enterprise_modules and d in enterprise_modules
               and edition != "enterprise"]
        unknown = [d for d in deps if d not in ent]
        parts = []
        if ent:
            kind = "an Odoo Enterprise module" if len(ent) == 1 else "Odoo Enterprise modules"
            parts.append(f"{_join(ent)} ({kind}: set the edition to Enterprise)")
        if unknown:
            where = "is" if len(unknown) == 1 else "are"
            parts.append(f"{_join(unknown)}, which {where} not in the repo, its dependencies or "
                         "Odoo (add the repo that provides it as a dependency)")
        lines.append(f"{module} cannot install: it depends on " + "; and on ".join(parts))
    return "\n".join(lines)


# Lines Odoo writes when it skips modules (Odoo 16-19 wording)
_UNMET = re.compile(r"module (\w+): Unmet dependencies: ([\w, ]+)")
_NOT_INSTALLABLE = re.compile(r"module (\w+): not installable, skipped")
_NOT_LOADED = re.compile(r"Some modules are not loaded, some dependencies or manifest may be missing: \[(.*?)\]")
_INVALID = re.compile(r"invalid module names, ignored: ([\w, ]+)")


def odoo_dependency_errors(log: str) -> list[str]:
    """Readable messages for the module problems Odoo reported in a build log."""
    out = []
    for m, deps in _UNMET.findall(log):
        out.append(f"{m} was not installed: Odoo could not find its dependencies {deps.strip()}")
    for m in _NOT_INSTALLABLE.findall(log):
        out.append(f"{m} is not installable on this Odoo version (manifest 'installable' is "
                   "False, or its version is for another Odoo series)")
    for names in _NOT_LOADED.findall(log):
        mods = ", ".join(n.strip(" '\"") for n in names.split(",") if n.strip())
        out.append(f"Odoo did not load {mods}: a dependency or manifest is missing")
    for names in _INVALID.findall(log):
        out.append(f"Unknown module name(s): {names.strip()} (not found in any addons path)")
    return list(dict.fromkeys(out))
