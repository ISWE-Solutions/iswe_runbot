"""SPEC "Build and run pipeline": one commit -> one running preview, in seven steps.
The previous preview of the branch stays up until the new one is healthy."""
import ast
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from .. import appsettings, credentials, deps, diagnose, gitmirror, modules as modplan, testresults
from ..config import get_settings
from ..db import session_scope
from ..models import Build, OdooVersion, utcnow
from ..naming import container_name, db_name as make_db_name
from ..security import random_secret
from . import docker_ops, notify, pg, templates
from .logs import BuildLog, error_summary, read_log

log = logging.getLogger(__name__)


class BuildFailed(Exception):
    """kind: "setup" (our own checks before Odoo), "missing_dependency" (install plan),
    "odoo" (Odoo failed; its log is diagnosed), "health" (preview never came up)."""

    def __init__(self, message: str, *, kind: str = "setup", hint: str = "",
                 signals: "diagnose.Signals | None" = None,
                 finding: "diagnose.Finding | None" = None):
        super().__init__(message)
        self.kind, self.hint, self.signals, self.finding = kind, hint, signals, finding


def access_failure(what: str, repo_url: str, account: str) -> BuildFailed:
    return BuildFailed(
        f"{what}: GitHub refused access to {gitmirror.repo_label(repo_url)} with {account}. "
        "The repository may not exist, or the token is expired or cannot read it (needs "
        "Contents: read). Check the account on the Git credentials page.")


@dataclass
class DepSpec:
    """A resolved dependency, snapshotted when the build starts (later edits don't apply)."""
    dep_id: int
    name: str        # technical module name when the repo root is a module
    repo_url: str
    branch: str
    source: str      # "branch" | "project" | "default" (see deps.resolve)
    token: str
    account: str     # credentials.Auth.label, for logs and errors (never the token)
    sha: str | None = None          # set once fetched
    root_is_module: bool = False    # repo root has __manifest__.py (exported into <name>/)

    @property
    def repo_label(self) -> str:
        return self.repo_url.rstrip("/").removesuffix(".git").split("github.com/")[-1]


def describe_source(source: str, version: str) -> str:
    return {
        "branch": "branch override, set in branch settings",
        "project": f"project mapping for {version}",
    }.get(source, f"default: branch named after Odoo {version}")


@dataclass
class BuildSpec:
    """Everything the pipeline needs, read once so no DB session stays open for 20 min."""
    build_id: int
    branch_id: int
    project_id: int
    host: str
    sha: str
    version: str
    base_image: str
    demo_flags: list[str]
    repo_url: str
    token: str
    account: str
    root_module: str
    enterprise: bool
    modules: list[str]
    deps: list[DepSpec]
    # Enterprise source for the version (empty repo = none configured)
    enterprise_repo: str
    enterprise_branch: str
    enterprise_token: str
    enterprise_account: str
    db_name: str
    mem_limit: str
    build_mem_limit: str
    cpus: float
    timeout_s: int
    health_timeout_s: int
    extra_modules: list[str] = field(default_factory=list)
    install: list[str] = field(default_factory=list)  # every module the plan installs
    # Combined preview: no repo of its own; installs the modules of all its parts
    combo: bool = False
    plain: bool = False
    project_name: str = ""
    edition: str = "community"
    # Community builds: where Enterprise module names could be looked up locally, to explain
    # a dependency on an Enterprise module (empty = unknown)
    enterprise_names_repo: str = ""
    enterprise_names_branch: str = ""
    branch_name: str = ""
    root_is_module: bool = False     # set when the project repo is exported
    demo: bool = False
    enterprise_dir: str = ""         # local Enterprise checkout, set when fetched
    run_tests: bool = False          # branch.effective_run_tests (never for plain previews)
    test_tags: str = ""              # Project.test_tags; blank = own modules
    test_failure_mode: str = "warn"  # "warn" | "fail"
    own_modules: list[str] = field(default_factory=list)  # project/parts modules being installed


def build_dir(build_id: int) -> Path:
    return get_settings().builds_dir / str(build_id)


def filestore_dir(build_id: int) -> Path:
    return get_settings().filestore_dir / str(build_id)


def split_modules(value: str | None) -> list[str]:
    return [m.strip() for m in (value or "").replace("\n", ",").split(",") if m.strip()]


def read_manifests(d: Path) -> dict[str, list[str]]:
    """{module: depends} for every module one level deep in an addons dir."""
    out = {}
    for mf in sorted(d.glob("*/__manifest__.py")):
        try:
            data = ast.literal_eval(mf.read_text(encoding="utf-8", errors="replace"))
            out[mf.parent.name] = [str(x) for x in (data.get("depends") or [])]
        except (ValueError, SyntaxError, AttributeError):
            out[mf.parent.name] = []
    return out


def plan_summary(plan: modplan.InstallPlan) -> str:
    repo = [m for m in plan.install if plan.sources.get(m) != "Odoo Enterprise"]
    ent = [m for m in plan.install if plan.sources.get(m) == "Odoo Enterprise"]
    extra = []
    if ent:
        extra.append(f"{', '.join(ent)} from Odoo Enterprise")
    if plan.core:
        extra.append(f"{len(plan.core)} Odoo core modules")
    tail = f" (+ {'; '.join(extra)})" if extra else ""
    return f"Install plan: asked for {', '.join(plan.requested)} -> installs {', '.join(repo)}{tail}"


def detect_modules(src: Path) -> list[str]:
    """Every folder one level deep containing __manifest__.py."""
    return sorted(p.parent.name for p in src.glob("*/__manifest__.py"))


def module_sources(dirs: list[tuple[str, Path]]) -> dict[str, list[str]]:
    """{module: [labels of the addons dirs providing it]} over (label, dir) pairs."""
    found: dict[str, list[str]] = {}
    for label, d in dirs:
        for m in detect_modules(d):
            found.setdefault(m, []).append(label)
    return found


def duplicate_modules(found: dict[str, list[str]]) -> str | None:
    """Odoo silently takes the first copy on the addons path; refuse instead."""
    dups = [(m, labels) for m, labels in sorted(found.items()) if len(labels) > 1]
    if not dups:
        return None
    return "; ".join(f"Module {m} is provided by both {' and '.join(labels)}" for m, labels in dups)


def place_export(export: Path, dest: Path, module_name: str) -> None:
    """Move an exported repo into `dest` (the addons dir). A repo whose root is itself a
    module goes to dest/<module_name> so Odoo sees the right technical name."""
    if dest.exists():
        shutil.rmtree(dest)
    if (export / "__manifest__.py").exists():
        dest.mkdir(parents=True)
        export.rename(dest / module_name)
    else:
        export.rename(dest)


def find_requirements(src: Path) -> Path | None:
    for p in [src / "requirements.txt", *src.glob("*/requirements.txt")]:
        if p.is_file() and p.read_text(errors="replace").strip():
            return p
    return None


def test_tags(spec: BuildSpec) -> str:
    """--test-tags value: the project's own setting, else "/<module>" per own module installed."""
    return spec.test_tags.strip() or ",".join(f"/{m}" for m in spec.own_modules)


def tests_enabled(spec: BuildSpec) -> bool:
    return spec.run_tests and not spec.plain and bool(test_tags(spec))


def init_args(spec: BuildSpec) -> list[str]:
    args = ["-d", spec.db_name, "-i", ",".join(spec.modules), "--stop-after-init", *spec.demo_flags]
    if tests_enabled(spec):
        args += ["--test-enable", "--test-tags", test_tags(spec)]
    return args


def load_spec(build_id: int) -> BuildSpec | None:
    """Mark the build `building` and snapshot it. None if it is no longer queued."""
    with session_scope() as s:
        b = s.get(Build, build_id)
        if b is None or b.status != "queued":
            return None
        branch, project = b.branch, b.branch.project
        version = s.get(OdooVersion, b.odoo_version)
        if version is None:
            raise BuildFailed(f"Odoo version {b.odoo_version} is not configured")
        demo = branch.effective_demo
        auth = credentials.for_project(project)
        ent = credentials.enterprise_source(s, b.odoo_version) if project.edition == "enterprise" else None
        names_src = credentials.enterprise_source(s, b.odoo_version) if ent is None else None
        dep_specs = []
        for r in deps.resolve(branch, b.odoo_version):
            dep_auth = credentials.for_dependency(r.dependency)
            dep_specs.append(DepSpec(r.dependency.id, r.dependency.root_module_name,
                                     r.dependency.repo_url, r.branch, r.source,
                                     dep_auth.token, dep_auth.label))
        b.status = "building"
        b.started_at = utcnow()
        b.host = branch.host
        b.db_name = make_db_name(project.slug, branch.slug, b.commit_sha, b.id)
        b.log_path = str(BuildLog(b.id).path)
        return BuildSpec(
            build_id=b.id, branch_id=branch.id, project_id=project.id, host=branch.host or "",
            sha=b.commit_sha, version=b.odoo_version, base_image=version.docker_image,
            demo_flags=(version.demo_flag_on if demo else version.demo_flag_off).split(),
            repo_url=project.repo_url, token=auth.token, account=auth.label,
            root_module=project.root_module_name, enterprise=project.edition == "enterprise",
            modules=split_modules(branch.effective_modules),
            deps=dep_specs,
            enterprise_repo=ent.repo_url if ent else "",
            enterprise_branch=ent.branch if ent else "",
            enterprise_token=ent.auth.token if ent else "",
            enterprise_account=ent.auth.label if ent else "",
            db_name=b.db_name,
            mem_limit=appsettings.get(s, "preview_memory_limit"),
            build_mem_limit=appsettings.get(s, "build_memory_limit"),
            cpus=float(appsettings.get(s, "preview_cpus")),
            timeout_s=appsettings.get_int(s, "build_timeout_minutes") * 60,
            health_timeout_s=appsettings.get_int(s, "health_timeout_seconds"),
            combo=project.is_combo,
            plain=project.is_plain,
            project_name=project.name,
            edition=project.edition,
            enterprise_names_repo=names_src.repo_url if names_src else "",
            enterprise_names_branch=names_src.branch if names_src else "",
            branch_name=branch.name,
            demo=demo,
            run_tests=branch.effective_run_tests and not project.is_plain,
            test_tags=project.test_tags or "",
            test_failure_mode=project.test_failure_mode or "warn",
        )


def run_build(build_id: int, followers: "Followers") -> None:
    try:
        spec = load_spec(build_id)
    except BuildFailed as e:
        findings = build_diagnosis(None, e, "")
        _mark_failed(build_id, with_headline(findings, str(e)), findings)
        return
    if spec is None:
        return
    blog = BuildLog(build_id)
    blog.step(f"Build {build_id}: commit {spec.sha[:7]} on Odoo {spec.version}")
    notify.build_event(build_id, "building")
    try:
        if not spec.host:
            raise BuildFailed("Branch has no preview host (host conflict?)")
        mounts = _prepare(spec, blog)
        _init_database(spec, mounts, blog)
        _start_and_check(spec, mounts, blog, followers)
    except Exception as e:  # noqa: BLE001 — any failure marks the build failed
        if isinstance(e, (BuildFailed, gitmirror.GitError)):
            message = str(e)
        else:
            message = f"{type(e).__name__}: {e}"
            log.exception("build %s failed", build_id)
        blog.step(f"FAILED: {message}")
        log_text = read_log(build_id)
        findings = build_diagnosis(spec, e, log_text)  # before teardown: needs nothing on disk
        teardown(build_id, spec.db_name, keep_log=True)
        _mark_failed(build_id, with_headline(findings, failure_summary(log_text, message)), findings)
        if findings:
            BuildLog(build_id).step("Why it failed: " + diagnose.headline(findings))
        return
    _mark_running_and_swap(spec, blog)


def _prepare(spec: BuildSpec, blog: BuildLog):
    bdir = build_dir(spec.build_id)
    bdir.mkdir(parents=True, exist_ok=True)

    # 1. Fetch code
    if spec.combo:
        if spec.plain and not spec.deps:
            blog.step(f"Plain Odoo {spec.version} ({spec.edition}): no custom code")
        elif not spec.deps:
            raise BuildFailed("This combined preview has no parts: add the repos to combine")
        else:
            blog.step(f"Combined preview of {len(spec.deps)} part(s): "
                      + ", ".join(f"{d.name}@{d.branch}" for d in spec.deps))
    else:
        blog.step(f"Fetching {gitmirror.repo_label(spec.repo_url)} with {spec.account}")
        mirror = gitmirror.project_mirror(spec.project_id)
        try:
            gitmirror.fetch_mirror(mirror, spec.repo_url, spec.token)
        except gitmirror.GitError as e:
            if gitmirror.is_access_error(e):
                raise access_failure("Project repository", spec.repo_url, spec.account) from None
            raise
        if not gitmirror.has_commit(mirror, spec.sha):
            raise BuildFailed(f"Commit {spec.sha[:7]} not found in the repository (force-pushed?)")
        gitmirror.export(mirror, spec.sha, bdir / "repo")
        spec.root_is_module = (bdir / "repo" / "__manifest__.py").exists()
        place_export(bdir / "repo", bdir / "src", spec.root_module)

    _fetch_dependencies(spec, bdir, blog)

    enterprise_dir = _fetch_enterprise(spec, blog) if spec.enterprise else None
    spec.enterprise_dir = str(enterprise_dir) if enterprise_dir else ""

    addon_dirs = [(d.repo_label, bdir / "extra" / str(i)) for i, d in enumerate(spec.deps)]
    if not spec.combo:
        addon_dirs.append((gitmirror.repo_label(spec.repo_url), bdir / "src"))
    clash = duplicate_modules(module_sources(addon_dirs))
    if clash:
        raise BuildFailed(clash)

    # 2. Prepare image
    req_dirs = [d for _, d in addon_dirs] if spec.combo else [bdir / "src"]
    requirements = next((r for r in map(find_requirements, req_dirs) if r), None)
    version_image = spec.base_image
    image = docker_ops.ensure_image(spec.base_image, spec.version, requirements, blog)
    with session_scope() as s:
        s.get(Build, spec.build_id).image = image
    spec.base_image = image

    # 3. Find modules and plan the install
    _plan_install(spec, bdir, enterprise_dir, blog, version_image)

    # The build's own Postgres role: owns only this build's database (worker/pg.py)
    db_password = random_secret()
    pg.ensure_role(pg.build_role(spec.build_id), db_password)
    (bdir / "odoo.conf").write_text(docker_ops.render_conf(
        db_name=spec.db_name, admin_passwd=random_secret(),
        db_user=pg.build_role(spec.build_id), db_password=db_password,
        addons=docker_ops.addons_path(len(spec.deps), enterprise_dir is not None,
                                      with_src=not spec.combo)))
    fs = filestore_dir(spec.build_id)
    fs.mkdir(parents=True, exist_ok=True)
    try:
        os.chown(fs, docker_ops.ODOO_UID, docker_ops.ODOO_UID)
    except OSError:
        pass  # e.g. Docker Desktop file sharing, where ownership is not enforced
    return docker_ops.mounts(bdir, fs, len(spec.deps), enterprise_dir, with_src=not spec.combo)


def failure_summary(log_text: str, message: str) -> str:
    """Odoo's module-dependency problems first, then our message, then ERROR lines."""
    lines = [*modplan.odoo_dependency_errors(log_text), *message.splitlines()]
    return error_summary(log_text, fallback="\n".join(dict.fromkeys(lines)))


SUMMARY_CAP = 2000


def with_headline(findings: list[diagnose.Finding], summary: str) -> str:
    """error_summary with the diagnosis headline first, kept under SUMMARY_CAP."""
    head = diagnose.headline(findings)
    if not head or summary.startswith(head):
        return summary[:SUMMARY_CAP]
    rest = summary[-max(0, SUMMARY_CAP - len(head) - 1):] if len(head) < SUMMARY_CAP else ""
    return (head + ("\n" + rest if rest else ""))[:SUMMARY_CAP]


def diagnosis_mounts(spec: BuildSpec) -> dict[str, diagnose.Source]:
    """In-container addons folders -> where they came from (for repo paths + GitHub links)."""
    out: dict[str, diagnose.Source] = {}
    if not spec.combo and spec.repo_url:
        out[docker_ops.SRC] = diagnose.Source(
            f"{gitmirror.repo_label(spec.repo_url)} @ {spec.branch_name or spec.sha[:7]}",
            spec.repo_url, spec.sha, root_module=spec.root_module if spec.root_is_module else "")
    for i, d in enumerate(spec.deps):
        out[f"{docker_ops.EXTRA}/{i}"] = diagnose.Source(
            f"{d.repo_label} @ {d.branch}", d.repo_url, d.sha or d.branch,
            root_module=d.name if d.root_is_module else "")
    if spec.enterprise and spec.enterprise_repo:
        out[docker_ops.ENTERPRISE] = diagnose.Source(
            f"Odoo Enterprise {gitmirror.repo_label(spec.enterprise_repo)} @ {spec.enterprise_branch}",
            spec.enterprise_repo, spec.enterprise_branch)
    return out


def _setup_hint(message: str) -> str:
    m = message.lower()
    if "git credentials page" in m or "refused access" in m:
        return ("Open Administration → Git credentials, press Test on the credential, and make sure its "
                "token can read this repository (Contents: read).")
    if "enterprise" in m:
        return ("Open Odoo versions and set (or Check) the Enterprise source for this version: the "
                "repository, its branch and a credential that can read it.")
    if m.startswith("dependency "):
        return ("Fix the branch on the project's Dependencies tab (version mapping) or in the branch "
                "settings (dependency branches).")
    if "no parts" in m:
        return "Add at least one repository on the Parts tab."
    if "provided by both" in m:
        return "Two repositories contain the same module: remove one of them from the dependencies or parts."
    if "commit" in m and "not found" in m:
        return "The branch was force-pushed: press Sync branches now, then Rebuild."
    if "host" in m:
        return "Another branch already uses this preview address; rename one of them (project slug)."
    if "odoo version" in m:
        return "Add or enable this Odoo version on the Odoo versions page."
    return "See the build log for details."


def build_diagnosis(spec: BuildSpec | None, err: Exception, log_text: str) -> list[diagnose.Finding]:
    """Findings for a failed build, most useful first (saved as Build.diagnosis)."""
    kind = getattr(err, "kind", "other" if not isinstance(err, gitmirror.GitError) else "setup")
    message = str(err) if isinstance(err, (BuildFailed, gitmirror.GitError)) else f"{type(err).__name__}: {err}"
    if getattr(err, "finding", None) is not None:
        return [err.finding]
    if kind == "missing_dependency":
        return [diagnose.Finding(
            "missing_dependency", "Missing module dependency", message,
            "Add the repository that provides the module as a dependency (Dependencies tab), choose the "
            "Enterprise edition for Enterprise modules, or remove it from the manifest's 'depends'.")]
    if kind == "setup":
        return [diagnose.Finding("setup", "Build setup failed", message, getattr(err, "hint", "") or
                                 _setup_hint(message))]
    signals = getattr(err, "signals", None) or diagnose.Signals(health_failed=kind == "health")
    found = diagnose.diagnose(log_text, diagnosis_mounts(spec) if spec else {}, signals)
    if spec and tests_enabled(spec):
        for f in found:
            if f.kind == "timeout":
                f.hint += (" Module tests are on and may need more time: raise the build time limit,"
                           " or turn tests off for this branch.")
    if found:
        return found
    if kind == "other":
        return [diagnose.Finding("other", "Unexpected error in the build worker", message,
                                 "This is a previewer bug, not a problem in your code: see the worker log.")]
    title = "Preview did not start" if kind == "health" else "Odoo could not install the modules"
    return [diagnose.Finding(kind if kind == "health" else "other", title, message,
                             "See the build log around the first ERROR line.")]


def _plan_install(spec: BuildSpec, bdir: Path, enterprise_dir: Path | None, blog: BuildLog,
                  core_image: str) -> None:
    """Work out what installs from the exported manifests, save it on the build, and fail
    before Odoo runs when a dependency is not available anywhere."""
    manifests: dict[str, list[str]] = {}
    sources: dict[str, str] = {}
    own: list[str] = []
    if enterprise_dir is not None:
        for m, d in read_manifests(enterprise_dir).items():
            manifests[m], sources[m] = d, "Odoo Enterprise"
    for i, dep in enumerate(spec.deps):
        found = read_manifests(bdir / "extra" / str(i))
        repo = dep.repo_label.rsplit("/", 1)[-1]
        for m, d in found.items():
            manifests[m], sources[m] = d, f"{repo} @ {dep.branch}"
        if spec.combo:
            own += list(found)
    if not spec.combo:
        found = read_manifests(bdir / "src")
        for m, d in found.items():
            manifests[m], sources[m] = d, f"{spec.project_name} repo"
        own = list(found)

    if not spec.modules:
        spec.modules = sorted(own) or (["base"] if spec.plain else [])
    if not spec.modules:
        raise BuildFailed("No modules found (no folder with __manifest__.py)")

    core = docker_ops.core_modules(core_image)
    if core is None:
        blog.write("Odoo core module list unavailable; unknown dependencies are assumed to be core\n")
    plan = modplan.plan(spec.modules, manifests, core, sources)
    plan.top_level = modplan.top_level({m: manifests[m] for m in own})
    spec.install = plan.install
    spec.own_modules = [m for m in plan.install if m in own]
    with session_scope() as s:
        s.get(Build, spec.build_id).install_plan = plan.as_dict()
    blog.step(plan_summary(plan))
    if not plan.ok:
        raise BuildFailed(modplan.explain(plan, _enterprise_names(spec, enterprise_dir), spec.edition),
                          kind="missing_dependency")


def _enterprise_names(spec: BuildSpec, enterprise_dir: Path | None) -> set[str] | None:
    """Enterprise module names, from this build's checkout or one already on disk."""
    d = enterprise_dir
    if d is None and spec.enterprise_names_repo:
        d = gitmirror.existing_enterprise_checkout(spec.enterprise_names_repo,
                                                   spec.enterprise_names_branch)
    return set(read_manifests(d)) if d is not None else None


def _fetch_enterprise(spec: BuildSpec, blog: BuildLog) -> Path:
    if not spec.enterprise_repo:
        raise BuildFailed(f"No Enterprise repository is set for Odoo {spec.version} (Odoo versions page)")
    repo = gitmirror.repo_label(spec.enterprise_repo)
    blog.step(f"Fetching Odoo Enterprise {spec.version}: {repo} @ {spec.enterprise_branch} "
              f"with {spec.enterprise_account} (first time is slow)")
    try:
        return gitmirror.enterprise_checkout(spec.enterprise_repo, spec.enterprise_branch,
                                             spec.enterprise_token)
    except gitmirror.GitError as e:
        if gitmirror.is_missing_ref(e):
            raise BuildFailed(
                f"Odoo Enterprise {spec.version}: {repo} has no {spec.enterprise_branch} branch "
                f"({spec.enterprise_account}). Set the Enterprise source for {spec.version} on the "
                "Odoo versions page.") from None
        if gitmirror.is_access_error(e):
            raise access_failure(f"Odoo Enterprise {spec.version}", spec.enterprise_repo,
                                 spec.enterprise_account) from None
        raise BuildFailed(f"Odoo Enterprise {spec.version} could not be fetched: {e}") from None


def _fetch_dependencies(spec: BuildSpec, bdir: Path, blog: BuildLog) -> None:
    """Export each dependency to extra/<n>, recording on the build what was used (a
    dependency that could not be resolved is recorded with sha None)."""
    refs: list[dict] = []

    def record(d: DepSpec, sha: str | None) -> None:
        refs.append({"name": d.name, "repo_url": d.repo_url, "branch": d.branch, "sha": sha,
                     "source": d.source})
        with session_scope() as s:
            s.get(Build, spec.build_id).dependency_refs = list(refs)

    for i, d in enumerate(spec.deps):
        where = describe_source(d.source, spec.version)
        blog.step(f"Fetching dependency {d.name}: {d.repo_label} @ {d.branch} ({where}) "
                  f"with {d.account}")
        emirror = gitmirror.extra_mirror(d.repo_url)
        try:
            gitmirror.fetch_mirror(emirror, d.repo_url, d.token)
        except gitmirror.GitError as e:
            record(d, None)
            if gitmirror.is_access_error(e):
                raise access_failure(f"Dependency {d.name}", d.repo_url, d.account) from None
            raise BuildFailed(f"Dependency {d.name}: cannot fetch {d.repo_label}: {e}") from None
        try:
            ref = gitmirror.rev_parse(emirror, f"refs/heads/{d.branch}")
        except gitmirror.GitError:
            record(d, None)
            raise BuildFailed(f"Dependency {d.name}: {d.repo_label.rsplit('/', 1)[-1]} has no branch "
                              f"{d.branch!r} ({where})") from None
        gitmirror.export(emirror, ref, bdir / "extra-repo")
        d.sha, d.root_is_module = ref, (bdir / "extra-repo" / "__manifest__.py").exists()
        place_export(bdir / "extra-repo", bdir / "extra" / str(i), d.name)
        blog.write(f"{d.name}: {d.repo_label} @ {d.branch} ({ref[:7]}) - {where}\n")
        record(d, ref)


def _from_template(spec: BuildSpec, blog: BuildLog) -> bool:
    """Create the build database by copying a ready template; False = install from scratch."""
    if not templates.enabled():
        return False
    t = templates.ensure(spec, blog)
    if t is None:
        return False
    started = time.monotonic()
    try:
        templates.copy_into(t, spec.db_name, filestore_dir(spec.build_id), pg.build_role(spec.build_id))
    except Exception as e:  # noqa: BLE001 — fall back to a full install
        blog.step(f"Copying the database template failed ({e}); installing from scratch")
        pg.drop_database(spec.db_name)
        return False
    flavour = f"{t.version} {t.edition.capitalize()}{' with demo' if t.demo else ''}"
    blog.step(f"Copied template {flavour} ({t.modules.replace(',', ', ')}) in "
              f"{time.monotonic() - started:.1f}s; installing {', '.join(spec.modules)}")
    return True


def _save_tests(build_id: int, status: str | None, results: dict) -> None:
    with session_scope() as s:
        b = s.get(Build, build_id)
        if b:
            b.test_status, b.test_results = status, results


def _record_tests(spec: BuildSpec) -> testresults.TestReport | None:
    """Parse and save the module test results of this build (None when tests are off)."""
    if not tests_enabled(spec):
        _save_tests(spec.build_id, "skipped", {})
        return None
    report = testresults.parse(read_log(spec.build_id), diagnosis_mounts(spec))
    _save_tests(spec.build_id, report.status, report.as_dict())
    return report


def _tests_finding(report: testresults.TestReport) -> diagnose.Finding:
    first = report.failures[0] if report.failures else None
    message = f"{first.test}: {first.message}" if first else report.headline
    return diagnose.Finding(
        "tests", f"Module tests failed: {report.headline}", message,
        "Fix the failing test(s), or set 'When tests fail' to keep the preview running.",
        module=first.module if first else None, file=first.file if first else None,
        line=first.line if first else None, source=first.source if first else None,
        url=first.url if first else None)


def _init_database(spec: BuildSpec, mounts, blog: BuildLog) -> None:
    # 4. Create database (from a template when one is available). Created here, owned by the
    # build's role, so that role never needs CREATEDB.
    if not _from_template(spec, blog):
        pg.create_empty_database(spec.db_name, pg.build_role(spec.build_id))
    blog.step(f"Creating database {spec.db_name}: odoo {' '.join(init_args(spec))}")
    if tests_enabled(spec):
        blog.step(f"Running tests: {test_tags(spec)}")
    outcome: dict = {}
    rc = docker_ops.run_init(image=spec.base_image, build_id=spec.build_id, args=init_args(spec),
                             mounts_=mounts, mem_limit=spec.build_mem_limit,
                             timeout_s=spec.timeout_s, log=blog, outcome=outcome)
    report = _record_tests(spec)
    tests_failed = report is not None and report.status == "failed"
    if tests_failed and (rc == 0 or (not outcome.get("timed_out") and not outcome.get("oom")
                                     and not _not_installed(spec))):
        # Odoo exits 1 when tests fail although every module installed: a test failure.
        if spec.test_failure_mode == "fail":
            raise BuildFailed(f"Module tests failed: {report.headline}", kind="tests",
                              finding=_tests_finding(report))
        blog.step(f"Tests failed (warn mode): {report.headline} — preview still starts")
        rc = 0  # the install itself succeeded; the module check below still runs
    elif report is not None and report.ran:
        blog.step(f"Tests: {report.headline}")
    if rc != 0:
        signals = diagnose.Signals(
            timed_out=bool(outcome.get("timed_out")), timeout_minutes=spec.timeout_s // 60,
            oom_killed=bool(outcome.get("oom")), memory_limit=spec.build_mem_limit,
            exit_code=outcome.get("exit_code", rc))
        raise BuildFailed(f"Module installation failed (exit code {rc})", kind="odoo", signals=signals)
    bad = _not_installed(spec)
    if bad:
        reasons = modplan.odoo_dependency_errors(read_log(spec.build_id))
        raise BuildFailed("\n".join(reasons) if reasons else
                          "Modules not installed: " + ", ".join(f"{m} ({st})" for m, st in bad.items()),
                          kind="odoo")


def _not_installed(spec: BuildSpec) -> dict[str, str]:
    """Planned modules that did not end up installed: {module: state}."""
    expected = list(dict.fromkeys([*spec.modules, *spec.install]))
    states = pg.module_states(spec.db_name, expected)
    return {m: states.get(m, "not found") for m in expected if states.get(m) != "installed"}


def _start_and_check(spec: BuildSpec, mounts, blog: BuildLog, followers: "Followers") -> None:
    # 5. Start preview, 6. Health check
    name = container_name(spec.build_id)
    from .automation import make_room
    slept = make_room(starting=spec.build_id)
    if slept:
        blog.step(f"Put {len(slept)} idle preview(s) to sleep to stay within the running limit")
    blog.step(f"Starting preview {name} at {spec.host}")
    ctr = docker_ops.run_preview(image=spec.base_image, build_id=spec.build_id, branch_id=spec.branch_id,
                                 host=spec.host, db_name=spec.db_name, mounts_=mounts,
                                 mem_limit=spec.mem_limit, cpus=spec.cpus,
                                 health_timeout_s=spec.health_timeout_s)
    with session_scope() as s:
        s.get(Build, spec.build_id).container_id = ctr.id
    followers.start(spec.build_id, from_start=True)
    problem = wait_until_healthy(name, spec.health_timeout_s, blog)
    if problem:
        state = docker_ops.container_state(name)
        signals = diagnose.Signals(health_failed=True, oom_killed=bool(state.get("oom")),
                                   memory_limit=spec.mem_limit, exit_code=state.get("exit_code"))
        raise BuildFailed(f"Preview did not become healthy: {problem}", kind="health", signals=signals)
    blog.step("Preview is healthy")


def wait_until_healthy(name: str, timeout_s: int, blog: BuildLog) -> str | None:
    """The start-up check, with one more wait of the same length when Odoo is still running
    but slow (rather than throwing away a fully installed database)."""
    problem = docker_ops.wait_healthy(name, timeout_s)
    if docker_ops.still_starting(name, problem):
        blog.step(f"Odoo is still starting after {timeout_s}s; waiting up to {timeout_s}s more")
        problem = docker_ops.wait_healthy(name, timeout_s)
    return problem


def _mark_running_and_swap(spec: BuildSpec, blog: BuildLog) -> None:
    # 7. Swap: retire the branch's previous builds
    with session_scope() as s:
        b = s.get(Build, spec.build_id)
        b.status = "running"
        b.finished_at = utcnow()
        old = s.scalars(select(Build).where(
            Build.branch_id == spec.branch_id, Build.id != spec.build_id,
            Build.status.in_(("running", "sleeping", "stopped")))).all()
        retire = [(o.id, o.db_name) for o in old]
    for old_id, old_db in retire:
        blog.step(f"Retiring previous build {old_id}")
        teardown(old_id, old_db, keep_log=True)
        with session_scope() as s:
            o = s.get(Build, old_id)
            o.status, o.container_id = "removed", None
    blog.step(f"Build {spec.build_id} is live at {spec.host}")
    notify.build_event(spec.build_id, "running")


def _mark_failed(build_id: int, summary: str,
                 findings: list[diagnose.Finding] | None = None) -> None:
    with session_scope() as s:
        b = s.get(Build, build_id)
        if b:
            b.status = "failed"
            b.error_summary = summary
            b.diagnosis = [f.as_dict() for f in findings or []]
            b.finished_at = utcnow()
            b.container_id = None
    notify.build_event(build_id, "failed")


def teardown(build_id: int, db_name: str | None, keep_log: bool = True) -> None:
    """Remove a build's containers, database, filestore and code (log kept)."""
    docker_ops.remove_container(f"{container_name(build_id)}-init")
    docker_ops.remove_container(container_name(build_id))
    if db_name:
        pg.drop_database(db_name)
    pg.drop_role(pg.build_role(build_id))
    shutil.rmtree(filestore_dir(build_id), ignore_errors=True)
    shutil.rmtree(build_dir(build_id), ignore_errors=True)
    if not keep_log:
        BuildLog(build_id).path.unlink(missing_ok=True)


class Followers:
    """One thread per running preview, appending its runtime output to the build log."""

    def __init__(self):
        self._threads: dict[int, threading.Thread] = {}
        self._lock = threading.Lock()

    def start(self, build_id: int, from_start: bool = False) -> None:
        with self._lock:
            t = self._threads.get(build_id)
            if t and t.is_alive():
                return
            t = threading.Thread(target=docker_ops.follow_logs,
                                 args=(container_name(build_id), BuildLog(build_id), from_start),
                                 daemon=True)
            self._threads[build_id] = t
            t.start()
