"""GitHub webhook: a push/create/delete on a repository enqueues a `sync` of every preview
that builds from it (the repo's project, and combined previews with a part on it). Sync then
rebuilds only selected branches with auto-rebuild on."""
import hashlib
import hmac
import json
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from ... import github, jobs
from ...db import SessionLocal
from ...models import Job, Project, ProjectDependency

router = APIRouter()
SYNC_EVENTS = ("push", "create", "delete")


def _norm(url: str) -> str | None:
    try:
        return github.clone_url(url).lower()
    except github.GitHubError:
        return None


def _payload(body: bytes, content_type: str) -> dict:
    if content_type.startswith("application/x-www-form-urlencoded"):
        return json.loads(parse_qs(body.decode()).get("payload", ["{}"])[0])
    return json.loads(body or b"{}")


def matching_projects(s, repo_urls: list[str]) -> list[Project]:
    """Repo projects whose own repo is one of `repo_urls`, plus combined previews with a part
    on one of them."""
    wanted = {u for u in map(_norm, repo_urls) if u}
    if not wanted:
        return []
    found = {p.id: p for p in s.scalars(select(Project).where(Project.kind == "repo"))
             if _norm(p.repo_url) in wanted}
    for dep in s.scalars(select(ProjectDependency).join(Project).where(Project.kind == "combo")):
        if _norm(dep.repo_url) in wanted:
            found.setdefault(dep.project_id, dep.project)
    return list(found.values())


def signature_ok(body: bytes, header: str | None, secret: str) -> bool:
    if not header or not secret or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len("sha256="):], expected)


def enqueue_sync(s, project_id: int) -> bool:
    """One queued sync per project is enough."""
    queued = s.scalars(select(Job).where(Job.type == "sync", Job.status == "queued")).all()
    if any((j.payload or {}).get("project_id") == project_id for j in queued):
        return False
    jobs.enqueue(s, "sync", {"project_id": project_id})
    return True


@router.post("/api/webhooks/github")
async def github_webhook(request: Request):
    body = await request.body()
    event = request.headers.get("x-github-event", "")
    sig = request.headers.get("x-hub-signature-256")
    try:
        data = _payload(body, request.headers.get("content-type", ""))
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "body is not a GitHub webhook payload"}, status_code=400)
    repo = data.get("repository") or {}
    urls = [u for u in (repo.get("clone_url"), repo.get("html_url"), repo.get("ssh_url")) if u]
    if event == "ping" and not urls:
        return JSONResponse({"ok": True, "message": "pong"})
    s = SessionLocal()
    try:
        projects = matching_projects(s, urls)
        if not projects:
            return JSONResponse({"error": f"no preview builds from {repo.get('full_name') or 'this repository'}"},
                                status_code=404)
        if not sig:
            return JSONResponse({"error": "missing X-Hub-Signature-256 (set the webhook secret)"},
                                status_code=401)
        if not any(signature_ok(body, sig, p.webhook_secret) for p in projects):
            return JSONResponse({"error": "signature does not match any project's webhook secret"},
                                status_code=401)
        if event == "ping":
            return JSONResponse({"ok": True, "message": "pong", "projects": [p.slug for p in projects]})
        if event not in SYNC_EVENTS:
            return JSONResponse({"ok": True, "ignored": event}, status_code=202)
        queued = [p.slug for p in projects if enqueue_sync(s, p.id)]
        s.commit()
        return JSONResponse({"ok": True, "event": event, "synced": queued,
                             "already_queued": [p.slug for p in projects if p.slug not in queued]})
    finally:
        s.close()
