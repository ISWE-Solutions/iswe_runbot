"""Why a build failed, for the screens. Uses Build.diagnosis (saved by the worker) and, for
builds that failed before the worker saved one, diagnoses the log file on the fly with the
same in-container mount -> repo mapping the worker uses."""
from pathlib import Path

from sqlalchemy.orm import object_session

from .. import credentials
from ..config import get_settings
from ..diagnose import Source, diagnose
from ..models import Build

MAX_LOG_BYTES = 4 * 1024 * 1024

KIND_STYLE = {
    # kind: (bootstrap icon, short label)
    "xml_syntax": ("bi-filetype-xml", "XML"),
    "view": ("bi-filetype-xml", "View"),
    "xml_data": ("bi-filetype-xml", "Data file"),
    "external_id": ("bi-filetype-xml", "Reference"),
    "access_csv": ("bi-shield-lock", "Access rights"),
    "python_syntax": ("bi-filetype-py", "Python"),
    "python_import": ("bi-filetype-py", "Python import"),
    "python_package": ("bi-filetype-py", "Python package"),
    "missing_dependency": ("bi-diagram-3", "Dependency"),
    "model_definition": ("bi-filetype-py", "Model"),
    "database": ("bi-database", "Database"),
    "out_of_memory": ("bi-memory", "Memory"),
    "timeout": ("bi-hourglass-split", "Timeout"),
    "health": ("bi-heart-pulse", "Start-up"),
    "setup": ("bi-gear", "Setup"),
    "other": ("bi-exclamation-octagon", "Error"),
}

# (build id, log mtime) -> findings; old failed builds never change, so this stays small
_cache: dict[tuple[int, float], list[dict]] = {}


def kind_icon(kind: str) -> str:
    return KIND_STYLE.get(kind, KIND_STYLE["other"])[0]


def kind_label(kind: str) -> str:
    return KIND_STYLE.get(kind, KIND_STYLE["other"])[1]


def _repo_label(url: str) -> str:
    return url.rstrip("/").removesuffix(".git").split("github.com/")[-1].lstrip(":")


def mounts_for(build: Build) -> dict[str, Source]:
    """In-container addons folders of this build -> where they came from."""
    branch = build.branch
    project = branch.project
    mounts: dict[str, Source] = {}
    if not project.is_combo and project.repo_url:
        mounts["/mnt/src"] = Source(f"{_repo_label(project.repo_url)} @ {branch.name}", project.repo_url,
                                    build.commit_sha, root_module=project.root_module_name)
    for i, ref in enumerate(build.dependency_refs or []):
        url = ref.get("repo_url") or ""
        mounts[f"/mnt/extra/{i}"] = Source(f"{_repo_label(url)} @ {ref.get('branch')}", url,
                                           ref.get("sha") or ref.get("branch") or "",
                                           root_module=ref.get("name") or "")
    if project.edition == "enterprise":
        src = None
        s = object_session(build)
        if s is not None:
            try:
                src = credentials.enterprise_source(s, build.odoo_version)
            except Exception:  # an unreadable credential must not break the page
                src = None
        if src:
            mounts["/mnt/enterprise"] = Source(f"Odoo Enterprise {_repo_label(src.repo_url)} @ {src.branch}",
                                               src.repo_url, src.branch)
        else:
            mounts["/mnt/enterprise"] = Source("Odoo Enterprise")
    return mounts


def _log_path(build: Build) -> Path:
    logs = get_settings().logs_dir.resolve()
    path = Path(build.log_path).resolve() if build.log_path else logs / f"{build.id}.log"
    return path if logs in path.parents else logs / f"{build.id}.log"


def _from_summary(summary: str) -> list[dict]:
    """Old builds whose log holds only our own setup message (no Odoo error to parse)."""
    from .module_view import dependency_lines
    lines = [ln.strip() for ln in summary.splitlines() if ln.strip()]
    if not lines:
        return []
    dep = dependency_lines(summary, 10)
    if dep:
        return [{"kind": "missing_dependency", "title": "Modules could not install", "message": "\n".join(dep),
                 "hint": "Add the repo that provides the missing module as a dependency, use Enterprise "
                         "for Enterprise modules, or remove it from 'depends' / the modules list."}]
    first = lines[0]
    if first.startswith("Modules not installed:"):
        return [{"kind": "missing_dependency", "title": "Module not found", "message": first,
                 "hint": "Odoo found no module with that name in the repo, its dependencies or Odoo: "
                         "check the modules list or module set of this branch."}]
    low = first.lower()
    if "enterprise" in low:
        hint = ("Check the Enterprise source of this Odoo version on the Odoo versions page (repo, "
                "branch and Git credential).")
    elif "refused access" in low or "credential" in low:
        hint = "Check the Git credential on the Git credentials page (Test shows what it can read)."
    else:
        hint = "See the log below for the step that failed."
    return [{"kind": "setup", "title": "Build setup failed", "message": "\n".join(lines[:3]), "hint": hint}]


def findings_for(build: Build) -> list[dict]:
    """Findings of a failed build, most useful first ([] for builds that did not fail)."""
    if build.status != "failed":
        return []
    if build.diagnosis:
        return list(build.diagnosis)
    path = _log_path(build)
    if not path.exists():
        return _from_summary(build.error_summary or "")
    key = (build.id, path.stat().st_mtime)
    if key not in _cache:
        with path.open("rb") as f:
            size = path.stat().st_size
            f.seek(max(0, size - MAX_LOG_BYTES))
            text = f.read().decode("utf-8", errors="replace")
        _cache[key] = ([f.as_dict() for f in diagnose(text, mounts_for(build))]
                       or _from_summary(build.error_summary or ""))
    return _cache[key]


def where(f: dict) -> str:
    if not f.get("file"):
        return ""
    return f"{f['file']}:{f['line']}" if f.get("line") else f["file"]


def first_finding(build: Build) -> dict | None:
    found = findings_for(build)
    return found[0] if found else None


def log_needles(f: dict) -> list[str]:
    """Strings to look for in the raw log to jump to the finding (best first)."""
    needles = []
    first = (f.get("message") or "").splitlines()[0].strip() if f.get("message") else ""
    for cand in (first, first.split(": ", 1)[-1] if ": " in first else "", f.get("file") or "",
                 f.get("module") or ""):
        cand = cand.strip()
        if len(cand) >= 4 and cand not in needles:
            needles.append(cand[:160])
    return needles
