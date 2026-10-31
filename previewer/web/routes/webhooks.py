"""GitHub webhook: a push/create/delete on a repository enqueues a `sync` of the previews that
build from it (the repo's project, and combined previews with a part on it) and whose own
secret signed the request. Sync then rebuilds only selected branches with auto-rebuild on.

The signature is always verified on the raw body before it is parsed or any project is looked
up by repository, so unsigned callers learn nothing and cost nothing."""
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
MAX_BODY = 25 * 1024 * 1024  # GitHub caps webhook payloads at 25 MB


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


async def read_body(request: Request) -> bytes | None:
    """The raw body, or None when it is larger than GitHub ever sends."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_BODY:
        return None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": "missing or wrong X-Hub-Signature-256 (check the webhook secret)"},
                        status_code=401)


def _too_large() -> JSONResponse:
    return JSONResponse({"error": "payload too large"}, status_code=413)


def _repo_urls(data: dict) -> tuple[dict, list[str]]:
    repo = data.get("repository") or {}
    return repo, [u for u in (repo.get("clone_url"), repo.get("html_url"), repo.get("ssh_url")) if u]


def _respond(s, event: str, projects: list[Project]) -> JSONResponse:
    if event == "ping":
        return JSONResponse({"ok": True, "message": "pong", "projects": [p.slug for p in projects]})
    if event not in SYNC_EVENTS:
        return JSONResponse({"ok": True, "ignored": event}, status_code=202)
    queued = [p.slug for p in projects if enqueue_sync(s, p.id)]
    s.commit()
    return JSONResponse({"ok": True, "event": event, "synced": queued,
                         "already_queued": [p.slug for p in projects if p.slug not in queued]})


@router.post("/api/webhooks/github/{project_id}")
async def project_webhook(project_id: int, request: Request):
    """Payload URL shown on each project: the secret is known before anything is parsed."""
    body = await read_body(request)
    if body is None:
        return _too_large()
    sig = request.headers.get("x-hub-signature-256")
    if not sig:
        return _unauthorized()
    s = SessionLocal()
    try:
        p = s.get(Project, project_id)
        if p is None or not signature_ok(body, sig, p.webhook_secret):
            return _unauthorized()
        try:
            data = _payload(body, request.headers.get("content-type", ""))
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "body is not a GitHub webhook payload"}, status_code=400)
        event = request.headers.get("x-github-event", "")
        repo, urls = _repo_urls(data)
        if event != "ping" and urls and p.id not in {q.id for q in matching_projects(s, urls)}:
            return JSONResponse({"ok": True, "ignored": f"{repo.get('full_name') or 'this repository'} "
                                 f"is not used by {p.slug}"}, status_code=202)
        return _respond(s, event, [p])
    finally:
        s.close()


@router.post("/api/webhooks/github")
async def github_webhook(request: Request):
    """Older shared payload URL: the signature is checked against every project's secret
    before the body is parsed, and only projects whose own secret signed it are synced."""
    body = await read_body(request)
    if body is None:
        return _too_large()
    sig = request.headers.get("x-hub-signature-256")
    if not sig:
        return _unauthorized()
    s = SessionLocal()
    try:
        signers = {p.id for p in s.scalars(select(Project).where(Project.webhook_secret_enc != ""))
                   if signature_ok(body, sig, p.webhook_secret)}
        if not signers:
            return _unauthorized()
        try:
            data = _payload(body, request.headers.get("content-type", ""))
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "body is not a GitHub webhook payload"}, status_code=400)
        event = request.headers.get("x-github-event", "")
        repo, urls = _repo_urls(data)
        if event == "ping" and not urls:
            return _respond(s, event, [s.get(Project, pid) for pid in sorted(signers)])
        projects = [p for p in matching_projects(s, urls) if p.id in signers]
        if not projects:
            return JSONResponse({"error": f"no preview builds from {repo.get('full_name') or 'this repository'}"},
                                status_code=404)
        return _respond(s, event, projects)
    finally:
        s.close()
