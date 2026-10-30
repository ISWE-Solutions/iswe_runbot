"""Wake-on-visit: a sleeping preview's host reaches the dashboard catch-all (its container is
stopped, so Traefik has no route for it). Start it once and show a page that reloads until the
preview answers."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import jobs
from ..models import Branch, Build, Job, User


def wake_pending(s: Session, build: Build) -> bool:
    return s.scalars(select(Job.id).where(
        Job.build_id == build.id, Job.type.in_(("wake", "start")),
        Job.status.in_(("queued", "running")))).first() is not None


def request_wake(s: Session, branch: Branch, build: Build) -> bool:
    """Enqueue one wake job for the build; False when one is already on its way."""
    if wake_pending(s, build):
        return False
    jobs.enqueue(s, "wake", {"build_id": build.id}, branch_id=branch.id, build_id=build.id)
    s.commit()
    return True


def stale_stopped_build(branch: Branch) -> Build | None:
    if branch.stale_since is None:
        return None
    return next((b for b in branch.builds if b.status == "stopped"), None)


def session_user(s: Session, session: dict) -> User | None:
    uid = session.get("user_id")
    user = s.get(User, uid) if isinstance(uid, int) else None
    return user if user and user.active else None
