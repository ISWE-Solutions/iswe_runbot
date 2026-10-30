import base64
import subprocess

import pytest

from previewer import gitmirror


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    (work / "__manifest__.py").write_text("{'name': 'x'}")
    (work / "models").mkdir()
    (work / "models" / "a.py").write_text("x = 1\n")
    _git("add", ".", cwd=work)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init", cwd=work)
    bare = tmp_path / "mirror.git"
    _git("clone", "-q", "--bare", str(work), str(bare), cwd=tmp_path)
    return bare


def test_export_and_has_commit(repo, tmp_path):
    sha = gitmirror.rev_parse(repo, "refs/heads/main")
    assert gitmirror.has_commit(repo, sha)
    assert not gitmirror.has_commit(repo, "0" * 40)
    dest = tmp_path / "out"
    gitmirror.export(repo, sha, dest)
    assert (dest / "__manifest__.py").exists() and (dest / "models" / "a.py").read_text() == "x = 1\n"


def test_auth_header_in_env_not_argv_and_redacted():
    env = gitmirror._auth_env("ghp_secret")
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    encoded = base64.b64encode(b"x-access-token:ghp_secret").decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {encoded}"
    assert gitmirror._auth_env("") == {}
    msg = gitmirror._redact(f"fatal ghp_secret {encoded}", "ghp_secret")
    assert "ghp_secret" not in msg and encoded not in msg


def test_git_error_is_redacted(tmp_path):
    with pytest.raises(gitmirror.GitError) as e:
        gitmirror.git(["rev-parse", "nope"], token="ghp_secret", git_dir=tmp_path / "missing.git")
    assert "ghp_secret" not in str(e.value)


def test_access_error_detection():
    assert gitmirror.is_access_error("remote: Repository not found.\nfatal: repository 'https://github.com/odoo/enterprise.git/' not found")
    assert gitmirror.is_access_error("fatal: Authentication failed for 'https://github.com/a/b.git/'")
    assert gitmirror.is_access_error("fatal: could not read Username for 'https://github.com': terminal prompts disabled")
    assert gitmirror.is_access_error("The requested URL returned error: 403")
    assert not gitmirror.is_access_error("fatal: couldn't find remote ref refs/heads/19.0")
    assert gitmirror.is_missing_ref("fatal: couldn't find remote ref refs/heads/19.0")


def test_enterprise_mirror_per_repo():
    a = gitmirror.enterprise_mirror("https://github.com/ISWE-Solutions/enterprise.git")
    b = gitmirror.enterprise_mirror("https://github.com/odoo/enterprise")
    assert a != b and a.name.startswith("enterprise-") and a.suffix == ".git"
    assert gitmirror.enterprise_mirror("github.com/odoo/enterprise.git") == b
    assert gitmirror.repo_label("https://github.com/odoo/enterprise.git") == "odoo/enterprise"


def test_mirror_lock_serialises_threads_and_clears_stale_git_locks(tmp_path):
    import threading
    import time
    mirror = tmp_path / "enterprise-x.git"
    mirror.mkdir()
    (mirror / "shallow.lock").write_text("left by a killed fetch")
    inside, overlaps = [], []

    def work():
        with gitmirror.mirror_lock(mirror):
            if inside:
                overlaps.append(1)
            inside.append(1)
            time.sleep(0.05)
            inside.pop()

    threads = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not overlaps and not (mirror / "shallow.lock").exists()
