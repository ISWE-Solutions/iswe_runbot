"""Postgres job queue (SELECT ... FOR UPDATE SKIP LOCKED). The web app only enqueues;
the worker claims and runs."""
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import Branch, Build, Job, utcnow


def enqueue(
    s: Session,
    type_: str,
    payload: dict | None = None,
    *,
    branch_id: int | None = None,
    build_id: int | None = None,
) -> Job:
    job = Job(type=type_, payload=payload or {}, branch_id=branch_id, build_id=build_id)
    s.add(job)
    s.flush()
    return job


def queue_build(s: Session, branch: Branch) -> Build:
    """Queue a build of the branch's last known commit with its effective settings.

    Any build of this branch still waiting in the queue is superseded (SPEC: a new
    commit cancels the branch's queued build). Caller commits.
    """
    if not branch.last_commit_sha:
        raise ValueError(f"Branch {branch.name!r} has no known commit; sync branches first")
    cancel_queued_builds(s, branch.id)
    build = Build(
        branch_id=branch.id,
        commit_sha=branch.last_commit_sha,
        odoo_version=branch.effective_version,
        status="queued",
        host=branch.host,
    )
    s.add(build)
    s.flush()
    enqueue(s, "build", {"build_id": build.id}, branch_id=branch.id, build_id=build.id)
    return build


def cancel_queued_builds(s: Session, branch_id: int) -> int:
    jobs = s.scalars(
        select(Job)
        .where(Job.branch_id == branch_id, Job.type == "build", Job.status == "queued")
        .with_for_update(skip_locked=True)
    ).all()
    for job in jobs:
        job.status = "cancelled"
        job.finished_at = utcnow()
        if job.build_id:
            s.execute(
                update(Build)
                .where(Build.id == job.build_id, Build.status == "queued")
                .values(status="removed", error_summary="Superseded by a newer build",
                        finished_at=utcnow())
            )
    return len(jobs)


def claim(s: Session, types: tuple[str, ...]) -> Job | None:
    """Atomically take the oldest runnable job of the given types. Commits."""
    job = s.scalars(
        select(Job)
        .where(Job.status == "queued", Job.type.in_(types), Job.run_after <= utcnow())
        .order_by(Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if job is None:
        s.rollback()
        return None
    job.status = "running"
    job.attempts += 1
    job.started_at = utcnow()
    s.commit()
    return job


def finish(s: Session, job: Job, error: str | None = None) -> None:
    job.status = "failed" if error else "done"
    job.error = error
    job.finished_at = utcnow()
    s.commit()
