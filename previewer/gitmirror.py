"""Bare git mirrors under DATA_DIR/repos and commit exports (worker only).

Tokens travel only as an `http.extraHeader` passed through git's GIT_CONFIG_* environment
for each call (not argv, so not visible in `ps`); never in a URL, git config file or log.
"""
import base64
import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

from .config import get_settings
from .github import clone_url


class GitError(Exception):
    pass


def _auth_env(token: str) -> dict[str, str]:
    if not token:
        return {}
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"}


def _redact(text: str, token: str) -> str:
    if token:
        text = text.replace(token, "***")
        text = text.replace(base64.b64encode(f"x-access-token:{token}".encode()).decode(), "***")
    return text


def git(args: list[str], *, token: str = "", git_dir: Path | None = None, timeout: int = 1800) -> str:
    cmd = ["git"]
    if git_dir is not None:
        cmd += ["--git-dir", str(git_dir)]
    cmd += args
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **_auth_env(token)}
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if p.returncode != 0:
        shown = " ".join(a for a in args)
        raise GitError(_redact(f"git {shown} failed: {p.stderr.strip()[-1500:]}", token))
    return p.stdout


def project_mirror(project_id: int) -> Path:
    return get_settings().repos_dir / f"project-{project_id}.git"


def extra_mirror(url: str) -> Path:
    return get_settings().repos_dir / f"extra-{_url_hash(url)[:12]}.git"


def _url_hash(url: str) -> str:
    return hashlib.sha1(clone_url(url).encode()).hexdigest()


def enterprise_mirror(url: str) -> Path:
    """One shallow mirror per Enterprise repo (Odoo versions may use different repos)."""
    return get_settings().repos_dir / f"enterprise-{_url_hash(url)[:12]}.git"


# git's wording when GitHub refuses the credentials or hides a private repo from them
_ACCESS_MARKERS = ("repository not found", "authentication failed", "could not read username",
                   "invalid username or token", "returned error: 401", "returned error: 403",
                   "permission to", "access denied")


def is_access_error(err: Exception | str) -> bool:
    text = str(err).lower()
    if "couldn't find remote ref" in text:
        return False
    return any(m in text for m in _ACCESS_MARKERS) or text.rstrip().endswith("not found")


def is_missing_ref(err: Exception | str) -> bool:
    return "couldn't find remote ref" in str(err).lower()


def repo_label(url: str) -> str:
    return url.rstrip("/").removesuffix(".git").split("github.com/")[-1].lstrip(":")


_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()
# Lock files git leaves behind when a fetch is killed; safe to remove while we hold the lock
_STALE_GIT_LOCKS = ("shallow.lock", "packed-refs.lock", "HEAD.lock", "config.lock", "FETCH_HEAD.lock")


@contextmanager
def mirror_lock(path: Path):
    """One writer per mirror: builds running in parallel threads (or another worker process)
    that fetch the same repo, e.g. a shared Enterprise mirror, take turns. Not re-entrant."""
    key = str(path)
    with _thread_locks_guard:
        tl = _thread_locks.setdefault(key, threading.Lock())
    with tl:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.parent / f"{path.name}.lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                for name in _STALE_GIT_LOCKS:
                    (path / name).unlink(missing_ok=True)
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _ensure_bare(path: Path, url: str) -> None:
    if (path / "HEAD").exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    git(["init", "--bare", "-q", str(path)])
    git(["remote", "add", "origin", url], git_dir=path)


def fetch_mirror(path: Path, url: str, token: str) -> None:
    """Full mirror of all branches (prunes deleted ones)."""
    with mirror_lock(path):
        _ensure_bare(path, clone_url(url))
        git(["fetch", "--prune", "-q", "origin", "+refs/heads/*:refs/heads/*"], token=token, git_dir=path)


def fetch_branch_shallow(path: Path, url: str, branch: str, token: str) -> str:
    """Depth-1 fetch of one branch; returns its head sha."""
    with mirror_lock(path):
        return _fetch_branch_shallow(path, url, branch, token)


def _fetch_branch_shallow(path: Path, url: str, branch: str, token: str) -> str:
    _ensure_bare(path, clone_url(url))
    git(["fetch", "--depth", "1", "-q", "origin", f"+refs/heads/{branch}:refs/heads/{branch}"],
        token=token, git_dir=path)
    return rev_parse(path, f"refs/heads/{branch}")


def remote_head(url: str, branch: str, token: str) -> str | None:
    """Head sha of a remote branch without fetching (git ls-remote); None if it is missing."""
    out = git(["ls-remote", "--heads", clone_url(url), f"refs/heads/{branch}"], token=token,
              timeout=60)
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/heads/{branch}":
            return sha
    return None


def rev_parse(path: Path, ref: str) -> str:
    return git(["rev-parse", ref], git_dir=path).strip()


def has_commit(path: Path, sha: str) -> bool:
    try:
        git(["cat-file", "-e", f"{sha}^{{commit}}"], git_dir=path)
        return True
    except GitError:
        return False


def export(path: Path, ref: str, dest: Path) -> None:
    """git archive <ref> | tar -x -C dest (dest is recreated)."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    archive = subprocess.Popen(["git", "--git-dir", str(path), "archive", "--format=tar", ref],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tar = subprocess.run(["tar", "-x", "-C", str(dest)], stdin=archive.stdout, capture_output=True)
    archive.stdout.close()
    err = archive.stderr.read().decode()
    if archive.wait() != 0 or tar.returncode != 0:
        raise GitError(f"export of {ref} failed: {err.strip() or tar.stderr.decode().strip()}")


def existing_enterprise_checkout(repo_url: str, branch: str) -> Path | None:
    """Newest complete local export of an Enterprise repo at `branch`, without fetching."""
    safe_branch = re.sub(r"[^A-Za-z0-9._-]+", "-", branch)
    prefix = f"enterprise-{_url_hash(repo_url)[:8]}-{safe_branch}-"
    found = [p for p in get_settings().checkouts_dir.glob(prefix + "*")
             if not p.name.endswith(".tmp") and (p / ".complete").exists()]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def enterprise_checkout(repo_url: str, branch: str, token: str) -> Path:
    """Shared read-only export of an Enterprise repo at `branch`, reused across builds."""
    c = get_settings()
    path = enterprise_mirror(repo_url)
    safe_branch = re.sub(r"[^A-Za-z0-9._-]+", "-", branch)
    with mirror_lock(path):  # fetch + export as one step: versions share this mirror
        sha = _fetch_branch_shallow(path, repo_url, branch, token)
        dest = c.checkouts_dir / f"enterprise-{_url_hash(repo_url)[:8]}-{safe_branch}-{sha[:12]}"
        if not (dest / ".complete").exists():
            tmp = dest.with_name(dest.name + ".tmp")
            export(path, sha, tmp)
            if dest.exists():
                shutil.rmtree(dest)
            tmp.rename(dest)
            (dest / ".complete").touch()
    # Older exports may still be mounted by running previews; Phase 2 cleanup removes them.
    return dest


def enterprise_checkouts() -> list[Path]:
    """Enterprise exports under DATA_DIR/checkouts (complete or not)."""
    d = get_settings().checkouts_dir
    return sorted(p for p in d.glob("enterprise-*") if p.is_dir()) if d.exists() else []
