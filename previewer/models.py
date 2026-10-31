"""Tables from SPEC.md "Data model". Change these together with a new Alembic migration."""
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .security import decrypt, encrypt


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


ROLES = ("admin", "developer", "tester")
EDITIONS = ("community", "enterprise")

# Build status values (SPEC.md). `removed` also covers queued builds superseded by a newer commit.
BUILD_STATUSES = ("queued", "building", "running", "sleeping", "failed", "stopped", "removed")
LIVE_STATUSES = ("running", "sleeping")

JOB_TYPES = ("build", "start", "stop", "wake", "delete", "sync", "pull")
JOB_STATUSES = ("queued", "running", "done", "failed", "cancelled")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="tester")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GithubCredential(Base):
    """A Git credential (user + token) the system pulls with. One is the default."""
    __tablename__ = "github_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    username: Mapped[str] = mapped_column(String(100), default="")
    token_enc: Mapped[str] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # Result of the last Test (GitHub /user), for the accounts page
    checked_login: Mapped[str | None] = mapped_column(String(100), nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    check_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OdooVersion(Base):
    __tablename__ = "odoo_versions"

    version: Mapped[str] = mapped_column(String(10), primary_key=True)
    docker_image: Mapped[str] = mapped_column(String(255))
    # Flags passed to `odoo -i` to turn demo data on/off; they differ by version.
    demo_flag_on: Mapped[str] = mapped_column(String(100), default="")
    demo_flag_off: Mapped[str] = mapped_column(String(100), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Where Enterprise addons come from for this version. Blank repo = ENTERPRISE_REPO_URL,
    # blank branch = the version name, NULL account = the default account.
    enterprise_repo_url: Mapped[str] = mapped_column(String(500), default="", server_default="")
    enterprise_branch: Mapped[str] = mapped_column(String(100), default="", server_default="")
    enterprise_credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("github_credentials.id", ondelete="SET NULL"), nullable=True)

    enterprise_credential: Mapped["GithubCredential | None"] = relationship()


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(63), unique=True)
    # "repo": one GitHub repo whose branches are previewed.
    # "combo": a combined preview with no repo of its own: its parts are the project
    # dependencies, pinned to branches on its single Branch row (COMBO_BRANCH).
    kind: Mapped[str] = mapped_column(String(10), default="repo", server_default="repo")
    repo_url: Mapped[str] = mapped_column(String(500), default="")
    # NULL = the default Git credential
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("github_credentials.id", ondelete="SET NULL"), nullable=True)
    default_odoo_version: Mapped[str] = mapped_column(ForeignKey("odoo_versions.version"))
    edition: Mapped[str] = mapped_column(String(20), default="community")
    # Technical module name used when the repo root itself is a module
    # (has __manifest__.py). Blank = repo name.
    module_name: Mapped[str] = mapped_column(String(100), default="")
    # Comma-separated; blank = auto-detect every module in the repo
    modules_to_install: Mapped[str] = mapped_column(Text, default="")
    # Named collection used by default (wins over modules_to_install)
    module_set_id: Mapped[int | None] = mapped_column(
        ForeignKey("module_sets.id", ondelete="SET NULL", use_alter=True), nullable=True)
    install_demo_data: Mapped[bool] = mapped_column(Boolean, default=False)
    # Sync lists every branch except those matching the exclude regex. New branches are
    # selected for building (Branch.enabled) only when they match auto_build_regex.
    auto_build_regex: Mapped[str] = mapped_column(String(255), default="")
    branch_exclude_regex: Mapped[str] = mapped_column(String(255), default="")
    # Fernet-encrypted like Git tokens; read and set through the `webhook_secret` property
    webhook_secret_enc: Mapped[str] = mapped_column(String(255), default="", server_default="")
    # Previews need a dashboard login (Traefik forward-auth); off = public client demo
    preview_auth_required: Mapped[bool] = mapped_column(Boolean, default=True)
    # Run the project's own module tests during the install (--test-enable)
    run_tests: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # "warn": failed tests keep the preview running with a warning; "fail": the build fails
    test_failure_mode: Mapped[str] = mapped_column(String(10), default="warn", server_default="warn")
    # Odoo --test-tags; blank = "/<module>" for each of the project's own modules
    test_tags: Mapped[str] = mapped_column(String(500), default="", server_default="")
    # Post build results as GitHub commit statuses (shows on pull requests)
    post_commit_status: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    credential: Mapped["GithubCredential | None"] = relationship()
    module_set: Mapped["ModuleSet | None"] = relationship(foreign_keys=[module_set_id], post_update=True)

    @property
    def webhook_secret(self) -> str:
        return decrypt(self.webhook_secret_enc or "")

    @webhook_secret.setter
    def webhook_secret(self, value: str) -> None:
        self.webhook_secret_enc = encrypt(value or "")
    module_sets: Mapped[list["ModuleSet"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", foreign_keys="ModuleSet.project_id",
        order_by="ModuleSet.name")
    branches: Mapped[list["Branch"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Branch.name"
    )
    dependencies: Mapped[list["ProjectDependency"]] = relationship(
        back_populates="project", cascade="all, delete-orphan",
        order_by="(ProjectDependency.position, ProjectDependency.id)",
    )

    @property
    def repo_name(self) -> str:
        return self.repo_url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]

    @property
    def root_module_name(self) -> str:
        return self.module_name or self.repo_name

    @property
    def is_combo(self) -> bool:
        """Combined previews and plain Odoo previews share the combo machinery (one
        COMBO_BRANCH row, host <slug>.<domain>); a plain one simply has no parts."""
        return self.kind in ("combo", "plain")

    @property
    def is_plain(self) -> bool:
        """Plain Odoo: a version/edition/demo choice with no custom code at all."""
        return self.kind == "plain"


# The one branch row of a combined preview
COMBO_BRANCH = "combined"


class ModuleSet(Base):
    """A named collection of modules to install, e.g. the three top-level modules that
    pull in the rest of a repo. Odoo installs their dependencies itself."""
    __tablename__ = "module_sets"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    modules: Mapped[str] = mapped_column(Text)  # comma-separated
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    project: Mapped["Project"] = relationship(back_populates="module_sets", foreign_keys=[project_id])

    @property
    def module_list(self) -> list[str]:
        return [m.strip() for m in self.modules.split(",") if m.strip()]


class ProjectDependency(Base):
    """Another repo placed on the addons path of every build of the project."""
    __tablename__ = "project_dependencies"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    repo_url: Mapped[str] = mapped_column(String(500))
    # Technical name when the repo root is a module; blank = repo name
    module_name: Mapped[str] = mapped_column(String(100), default="")
    # NULL = same account as the project
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("github_credentials.id", ondelete="SET NULL"), nullable=True)
    # {"18.0": "main", "19.0": "v19"}; a version not listed uses the branch named after it
    branch_map: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    project: Mapped["Project"] = relationship(back_populates="dependencies")
    credential: Mapped["GithubCredential | None"] = relationship()

    @property
    def repo_name(self) -> str:
        return self.repo_url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]

    @property
    def root_module_name(self) -> str:
        return self.module_name or self.repo_name

    def branch_for(self, version: str) -> str:
        return (self.branch_map or {}).get(version) or version


class Branch(Base):
    __tablename__ = "branches"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(63))
    # Preview host, e.g. payroll-v18.preview.example.com. NULL while host_conflict is set.
    host: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    host_conflict: Mapped[str | None] = mapped_column(Text, nullable=True)
    odoo_version_override: Mapped[str | None] = mapped_column(
        ForeignKey("odoo_versions.version"), nullable=True
    )
    modules_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_tests_override: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    module_set_id: Mapped[int | None] = mapped_column(
        ForeignKey("module_sets.id", ondelete="SET NULL"), nullable=True)
    demo_data_override: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Selected for building. Unselected branches are listed but never built.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Off = new commits do not rebuild (the preview stays on its current build); manual
    # Rebuild still works
    auto_rebuild: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # Per-branch dependency branches: {"<dependency id>": "<branch name>"}; wins over the
    # dependency's per-version mapping (see previewer/deps.py)
    dependency_branches: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    # Starred long-lived branches (versions, main) listed first on the board
    sticky: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    last_commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_commit_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_commit_author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_commit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when cleanup stops a branch with no new commits for stale_after_days
    stale_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    project: Mapped[Project] = relationship(back_populates="branches")
    module_set: Mapped["ModuleSet | None"] = relationship(foreign_keys=[module_set_id])
    builds: Mapped[list["Build"]] = relationship(
        back_populates="branch", cascade="all, delete-orphan", order_by="Build.id.desc()"
    )

    @property
    def effective_version(self) -> str:
        return self.odoo_version_override or self.project.default_odoo_version

    @property
    def effective_modules(self) -> str:
        """Comma-separated list, blank = auto-detect. Order: branch modules override >
        branch module set > project module set > project modules list."""
        if self.modules_override is not None and self.modules_override.strip():
            return self.modules_override
        if self.module_set is not None:
            return self.module_set.modules
        if self.project.module_set is not None:
            return self.project.module_set.modules
        return self.project.modules_to_install or ""

    @property
    def effective_run_tests(self) -> bool:
        if self.run_tests_override is not None:
            return self.run_tests_override
        return self.project.run_tests

    @property
    def effective_demo(self) -> bool:
        if self.demo_data_override is not None:
            return self.demo_data_override
        return self.project.install_demo_data

    @property
    def live_build(self) -> "Build | None":
        """The build currently serving (or able to serve) the preview."""
        return next((b for b in self.builds if b.status in LIVE_STATUSES), None)

    @property
    def latest_build(self) -> "Build | None":
        return self.builds[0] if self.builds else None


class Build(Base):
    __tablename__ = "builds"

    id: Mapped[int] = mapped_column(primary_key=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"))
    commit_sha: Mapped[str] = mapped_column(String(40))
    odoo_version: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    container_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    db_name: Mapped[str | None] = mapped_column(String(63), nullable=True)
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    log_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What the build fetched besides the branch itself, set by the worker:
    # [{"name": "bank_zm", "repo_url": ..., "branch": "main", "sha": ..., "source": "project"}]
    dependency_refs: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    # Set by the worker before Odoo runs (see previewer/modules.py InstallPlan.as_dict):
    # {"requested": [...], "install": [...], "core": [...], "missing": {module: [deps]},
    #  "sources": {module: "payroll_zm repo"}, "top_level": [...]}
    install_plan: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    # Module tests: "passed" | "failed" | "skipped" (tests off / nothing to test) | None (not run yet)
    test_status: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # testresults.TestReport.as_dict(): totals, failures with file/line/url/traceback, skipped tests
    test_results: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    # Why it failed, set by the worker on failure: [diagnose.Finding.as_dict(), ...] most useful first
    diagnosis: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")

    branch: Mapped[Branch] = relationship(back_populates="builds")

    @property
    def short_sha(self) -> str:
        return (self.commit_sha or "")[:7]


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    branch_id: Mapped[int | None] = mapped_column(
        ForeignKey("branches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    build_id: Mapped[int | None] = mapped_column(
        ForeignKey("builds.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class SchedulerState(Base):
    """Last run of each periodic worker task (survives worker restarts)."""
    __tablename__ = "scheduler_state"

    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    last_message: Mapped[str] = mapped_column(Text, default="")


class DbTemplate(Base):
    """A ready-made database (base + Enterprise core installed) that builds copy instead of
    installing Odoo from scratch. One per version / edition / demo / image / Enterprise commit."""
    __tablename__ = "db_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)
    version: Mapped[str] = mapped_column(String(10))
    edition: Mapped[str] = mapped_column(String(20))
    demo: Mapped[bool] = mapped_column(Boolean)
    image: Mapped[str] = mapped_column(String(255))
    enterprise_ref: Mapped[str] = mapped_column(String(80), default="")
    db_name: Mapped[str] = mapped_column(String(63))
    modules: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="building")  # building | ready | failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
