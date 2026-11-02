"""Minimal GitHub REST client: test a repo connection and list its branches."""
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

import httpx

API = "https://api.github.com"


class GitHubError(Exception):
    pass


@dataclass
class BranchInfo:
    name: str
    sha: str
    message: str | None = None
    author: str | None = None
    committed_at: datetime | None = None


def parse_repo(url: str) -> tuple[str, str]:
    """'https://github.com/acme/x.git' | 'github.com/acme/x' | 'git@github.com:acme/x' -> (acme, x)."""
    m = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?/?$", url.strip())
    if not m:
        raise GitHubError(f"Not a GitHub repository URL: {url}")
    return m.group(1), m.group(2)


def clone_url(url: str) -> str:
    owner, repo = parse_repo(url)
    return f"https://github.com/{owner}/{repo}.git"


def _client(token: str) -> httpx.Client:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=API, headers=headers, timeout=20)


def _check(r: httpx.Response, what: str) -> None:
    if r.status_code == 401:
        raise GitHubError("GitHub rejected the token (401)")
    if r.status_code == 404:
        raise GitHubError(f"{what} not found, or the token cannot see it (404)")
    if r.status_code >= 400:
        raise GitHubError(f"GitHub error {r.status_code} on {what}: {r.text[:200]}")


def test_connection(repo_url: str, token: str) -> dict:
    """Returns {'full_name', 'private', 'default_branch'}; raises GitHubError."""
    owner, repo = parse_repo(repo_url)
    with _client(token) as c:
        r = c.get(f"/repos/{owner}/{repo}")
        _check(r, f"{owner}/{repo}")
        d = r.json()
    return {"full_name": d["full_name"], "private": d["private"], "default_branch": d["default_branch"]}


def list_branches(repo_url: str, token: str) -> list[BranchInfo]:
    """All branches with their head sha (no commit details; see fill_commit_details)."""
    owner, repo = parse_repo(repo_url)
    out: list[BranchInfo] = []
    with _client(token) as c:
        page = 1
        while True:
            r = c.get(f"/repos/{owner}/{repo}/branches", params={"per_page": 100, "page": page})
            _check(r, f"{owner}/{repo} branches")
            items = r.json()
            out += [BranchInfo(name=b["name"], sha=b["commit"]["sha"]) for b in items]
            if len(items) < 100:
                break
            page += 1
    return out


def fill_commit_details(repo_url: str, token: str, branches: list[BranchInfo]) -> None:
    """Set message/author/committed_at on each branch (one API call per branch)."""
    owner, repo = parse_repo(repo_url)
    with _client(token) as c:
        for b in branches:
            r = c.get(f"/repos/{owner}/{repo}/commits/{b.sha}")
            if r.status_code != 200:
                continue
            commit = r.json()["commit"]
            b.message = (commit["message"] or "").splitlines()[0][:500] if commit["message"] else ""
            b.author = commit["author"]["name"]
            b.committed_at = datetime.fromisoformat(commit["author"]["date"].replace("Z", "+00:00"))


def branch_head(repo_url: str, token: str, branch: str) -> BranchInfo | None:
    """The branch's latest commit with its details (one API call); None if the branch is gone.
    Raises GitHubError when the repo itself is unreachable."""
    owner, repo = parse_repo(repo_url)
    with _client(token) as c:
        r = c.get(f"/repos/{owner}/{repo}/branches/{quote(branch, safe='')}")
        if r.status_code == 404:
            _check(c.get(f"/repos/{owner}/{repo}"), f"{owner}/{repo}")
            return None
        _check(r, f"{owner}/{repo} branch {branch}")
        head = r.json()["commit"]
    commit = head["commit"]
    message = (commit.get("message") or "").splitlines()
    author = commit.get("author") or {}
    return BranchInfo(name=branch, sha=head["sha"], message=message[0][:500] if message else "",
                      author=author.get("name"),
                      committed_at=datetime.fromisoformat(author["date"].replace("Z", "+00:00"))
                      if author.get("date") else None)


def branch_exists(repo_url: str, token: str, branch: str) -> bool:
    """True if the branch exists; raises GitHubError when the repo itself is unreachable."""
    owner, repo = parse_repo(repo_url)
    with _client(token) as c:
        r = c.get(f"/repos/{owner}/{repo}/branches/{branch}")
        if r.status_code == 404:
            probe = c.get(f"/repos/{owner}/{repo}")
            _check(probe, f"{owner}/{repo}")
            return False
        _check(r, f"{owner}/{repo} branch {branch}")
        return True


def get_file(repo_url: str, token: str, path: str, ref: str) -> str | None:
    """Raw file contents at ref, or None if the file does not exist."""
    owner, repo = parse_repo(repo_url)
    with _client(token) as c:
        r = c.get(f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref},
                  headers={"Accept": "application/vnd.github.raw+json"})
        if r.status_code == 404:
            return None
        _check(r, f"{owner}/{repo}:{path}@{ref}")
        return r.text


def top_level_dirs(repo_url: str, token: str, ref: str) -> list[str] | None:
    """Folder names at the repo root, or None if the ref does not exist."""
    owner, repo = parse_repo(repo_url)
    with _client(token) as c:
        r = c.get(f"/repos/{owner}/{repo}/contents/", params={"ref": ref})
        if r.status_code == 404:
            return None
        _check(r, f"{owner}/{repo}@{ref}")
        return [e["name"] for e in r.json() if e["type"] == "dir"]


def list_dirs(repo_url: str, token: str, path: str, ref: str) -> list[str] | None:
    """Folder names inside `path` at ref, or None if the path/ref does not exist."""
    owner, repo = parse_repo(repo_url)
    with _client(token) as c:
        r = c.get(f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})
        if r.status_code == 404:
            return None
        _check(r, f"{owner}/{repo}:{path}@{ref}")
        return [e["name"] for e in r.json() if e["type"] == "dir"]


def create_commit_status(repo_url: str, token: str, sha: str, state: str, context: str,
                         description: str, target_url: str | None = None, timeout: float = 5) -> None:
    """Commit status (shown on pull requests). state: pending | success | failure | error."""
    owner, repo = parse_repo(repo_url)
    body = {"state": state, "context": context, "description": description[:140]}
    if target_url:
        body["target_url"] = target_url
    with _client(token) as c:
        r = c.post(f"/repos/{owner}/{repo}/statuses/{sha}", json=body, timeout=timeout)
        _check(r, f"{owner}/{repo} commit status")
