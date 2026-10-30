"""Choosing which branches build, gated by the dependency check (previewer/deps.py).

A failed check leaves the branch selected but unbuilt; the failing results are kept in
memory so the branch picker can show "Blocked: ..." with Check again / Build anyway.
"""
import time

from fastapi import Request
from sqlalchemy import select as sa_select
from sqlalchemy.orm import Session, object_session

from .. import deps as builddeps
from .. import github, jobs
from ..models import Branch, Job
from .deps import flash

BLOCK_TTL = 6 * 3600
_blocked: dict[int, tuple[float, list[builddeps.CheckResult]]] = {}


def run_check(branch: Branch, version: str | None = None) -> list[builddeps.CheckResult]:
    try:
        results = builddeps.check(branch, version)
        if branch.project.is_combo and branch.project.edition == "community":
            extra = combo_community_check(branch, version or branch.effective_version)
            if extra:
                results.append(extra)
        return results
    except Exception as e:  # noqa: BLE001  (network errors reaching GitHub)
        return [builddeps.CheckResult("GitHub", "", "", False,
                                      f"Could not reach GitHub: {e.__class__.__name__}")]


def combo_community_check(branch: Branch, version: str) -> builddeps.CheckResult | None:
    """The Enterprise safeguard for a Community combined preview: its parts' manifests must not
    depend on Enterprise-only modules. deps.check skips it for combos (no repo of their own)."""
    s = object_session(branch)
    try:
        ent = builddeps.enterprise_modules(s, version)
        if ent is None:
            return None
        provided: dict[str, list[str]] = {}
        for r in builddeps.resolve(branch, version):
            d = r.dependency
            provided.update(builddeps.repo_manifests(d.repo_url, builddeps.dependency_token(d),
                                                     r.branch, d.root_module_name))
    except github.GitHubError:
        return None
    needs = {m: sorted(x for x in depends if x in ent and x not in provided)
             for m, depends in provided.items()}
    needs = {m: x for m, x in needs.items() if x}
    if not needs:
        return None
    return builddeps.CheckResult("Odoo Community", "", version, False, builddeps.describe_needs(needs))


def blocked(branch: Branch) -> list[builddeps.CheckResult]:
    """Failing check results for a selected branch that was not queued because of them."""
    entry = _blocked.get(branch.id)
    if not entry or not branch.enabled or time.time() - entry[0] > BLOCK_TTL:
        return []
    return [r for r in entry[1] if not r.ok]


def clear_blocked(branch_id: int) -> None:
    _blocked.pop(branch_id, None)


def queue(request: Request, s: Session, b: Branch) -> bool:
    """Queue a build without checking dependencies (Rebuild, Build anyway)."""
    if b.deleted_at:
        flash(request, f"{b.name} was deleted on GitHub; it cannot be rebuilt.", "error")
        return False
    if not b.host:
        flash(request, f"{b.name}: {b.host_conflict or 'no preview host assigned'}", "error")
        return False
    if b.project.is_combo and not b.last_commit_sha:
        # The parts' heads are not known yet: the worker's sync of a combo resolves them
        # (the combined commit) and queues the first build.
        refresh_combo(s, b.project_id)
        clear_blocked(b.id)
        return True
    try:
        jobs.queue_build(s, b)
    except ValueError as e:
        flash(request, str(e), "error")
        return False
    clear_blocked(b.id)
    return True


def queue_checked(request: Request, s: Session, b: Branch, *, quiet: bool = False) -> str:
    """Check dependencies, then queue. Returns 'queued', 'blocked' or 'skipped'."""
    if b.project.is_combo:
        outcome = queue_combo(request, s, b)
        if outcome == "blocked" and not quiet:
            flash(request, f"{b.project.name} is blocked: " + "; ".join(
                r.message for r in blocked(b)), "error")
        return outcome
    if not b.last_commit_sha or b.deleted_at or not b.host:
        if not quiet:
            queue(request, s, b)  # flashes the reason
        return "skipped"
    results = run_check(b)
    problems = [r for r in results if not r.ok]
    if problems:
        _blocked[b.id] = (time.time(), results)
        if not quiet:
            flash(request, f"{b.name} is selected but blocked: "
                  + "; ".join(r.message for r in problems), "error")
        return "blocked"
    return "queued" if queue(request, s, b) else "skipped"


def refresh_combo(s: Session, project_id: int) -> None:
    """Queue the combo's `sync` (refresh the parts' heads) unless one is already waiting."""
    waiting = s.scalars(sa_select(Job).where(Job.type == "sync", Job.status == "queued")).all()
    if not any((j.payload or {}).get("project_id") == project_id for j in waiting):
        jobs.enqueue(s, "sync", {"project_id": project_id})


def mark_blocked(branch: Branch, results: list[builddeps.CheckResult]) -> None:
    _blocked[branch.id] = (time.time(), results)


def queue_combo(request: Request, s: Session, b: Branch, *, check: bool = True) -> str:
    """Combined preview: check its parts, then build (or refresh the parts first when their
    heads are unknown). Returns 'queued', 'blocked' or 'skipped'."""
    if check:
        results = run_check(b)
        if any(not r.ok for r in results):
            mark_blocked(b, results)
            return "blocked"
    return "queued" if queue(request, s, b) else "skipped"


def select(request: Request, s: Session, b: Branch, *, quiet: bool = False) -> str:
    b.enabled = True
    s.flush()
    return queue_checked(request, s, b, quiet=quiet)


def unselect(s: Session, b: Branch) -> None:
    b.enabled = False
    clear_blocked(b.id)
    jobs.cancel_queued_builds(s, b.id)
    live = b.live_build
    if live:
        jobs.enqueue(s, "stop", {"build_id": live.id}, branch_id=b.id, build_id=live.id)


def dep_summary(b: Branch) -> list[dict]:
    """What a build of this branch would fetch (no network)."""
    out = [{"name": r.dependency.root_module_name, "branch": r.branch,
            "source": "part" if b.project.is_combo else r.source,
            "repo_url": r.dependency.repo_url} for r in builddeps.resolve(b)]
    if b.project.edition == "enterprise":
        out.append({"name": "Enterprise", "branch": b.effective_version, "source": "enterprise",
                    "repo_url": ""})
    return out
