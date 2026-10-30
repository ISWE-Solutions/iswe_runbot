from datetime import datetime, timezone

import pytest

from previewer.github import BranchInfo
from previewer import github
from previewer.models import Branch, Build, GithubCredential, Job, Project
from previewer.security import encrypt
from previewer.worker import sync


@pytest.fixture
def fake_github(monkeypatch):
    remote: list[BranchInfo] = []
    monkeypatch.setattr(sync.github, "list_branches", lambda url, token: [BranchInfo(b.name, b.sha) for b in remote])

    def fill(url, token, branches):
        for b in branches:
            b.message, b.author = f"msg {b.name}", "dev"
            b.committed_at = datetime(2026, 10, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(sync.github, "fill_commit_details", fill)
    return remote


def _project(s, slug="payroll", **kw):
    if not s.query(GithubCredential).count():
        s.add(GithubCredential(name="Default", username="bot", token_enc=encrypt("tok"), is_default=True))
    p = Project(name=slug.title(), slug=slug, repo_url=f"https://github.com/acme/{slug}",
                default_odoo_version="18.0", **kw)
    s.add(p)
    s.commit()
    return p


def test_branch_listed_and_auto_build():
    assert sync.branch_listed("v18-beta", "")
    assert not sync.branch_listed("tmp-x", "^tmp")
    assert sync.auto_build("v18", "^v18$")
    assert not sync.auto_build("v18-beta", "^v18$")
    assert not sync.auto_build("anything", "")


def test_sync_lists_all_selects_by_regex_and_queues(s, fake_github):
    p = _project(s, auto_build_regex="^v1[89]$", branch_exclude_regex="^tmp")
    fake_github += [BranchInfo("v18", "a" * 40), BranchInfo("v18-beta", "b" * 40),
                    BranchInfo("v19", "c" * 40), BranchInfo("tmp-x", "d" * 40)]
    counts = sync.sync_project(s, p.id)
    s.commit()
    assert counts["new"] == 3 and counts["selected_new"] == 2 and counts["queued"] == 2
    branches = {b.name: b for b in s.query(Branch).all()}
    assert set(branches) == {"v18", "v18-beta", "v19"}
    assert branches["v18"].enabled and branches["v19"].enabled and not branches["v18-beta"].enabled
    assert branches["v18-beta"].host == "payroll-v18-beta.preview.localhost"
    assert branches["v18-beta"].last_commit_message == "msg v18-beta"
    assert s.query(Build).filter_by(status="queued").count() == 2
    assert s.query(Job).filter_by(type="build").count() == 2

    # Same heads again: nothing new to build.
    counts = sync.sync_project(s, p.id)
    s.commit()
    assert counts["queued"] == 0

    # The user's choice sticks: deselecting v19 and selecting v18-beta survive a sync, and
    # only selected branches with new commits build.
    branches["v19"].enabled = False
    branches["v18-beta"].enabled = True
    s.commit()
    fake_github[:] = [BranchInfo("v18", "e" * 40), BranchInfo("v18-beta", "f" * 40),
                      BranchInfo("v19", "9" * 40)]
    counts = sync.sync_project(s, p.id)
    s.commit()
    s.expire_all()
    assert counts["updated"] == 3 and counts["queued"] == 2
    assert not s.query(Branch).filter_by(name="v19").one().enabled
    assert s.query(Branch).filter_by(name="v18-beta").one().enabled

    # A vanished branch is marked deleted; the older queued v18 build was superseded.
    fake_github[:] = [BranchInfo("v18", "e" * 40), BranchInfo("v18-beta", "f" * 40)]
    counts = sync.sync_project(s, p.id)
    s.commit()
    assert counts["deleted"] == 1
    v18_builds = s.query(Build).join(Branch).filter(Branch.name == "v18").order_by(Build.id).all()
    assert [b.status for b in v18_builds] == ["removed", "queued"]


def test_sync_excluded_branch_is_marked_deleted(s, fake_github):
    p = _project(s)
    fake_github[:] = [BranchInfo("v18", "a" * 40), BranchInfo("wip", "b" * 40)]
    sync.sync_project(s, p.id)
    s.commit()
    assert s.query(Branch).count() == 2 and s.query(Build).count() == 0  # nothing selected
    p.branch_exclude_regex = "^wip$"
    s.commit()
    counts = sync.sync_project(s, p.id)
    s.commit()
    assert counts["deleted"] == 1
    assert s.query(Branch).filter_by(name="wip").one().deleted_at is not None


def test_sync_host_conflict(s, fake_github):
    # "a-b" project + branch "c" and "a" project + branch "b-c" both want a-b-c.
    p1 = _project(s, slug="a-b")
    p2 = _project(s, slug="a")
    fake_github[:] = [BranchInfo("c", "a" * 40)]
    sync.sync_project(s, p1.id)
    s.commit()
    fake_github[:] = [BranchInfo("b/c", "b" * 40)]
    counts = sync.sync_project(s, p2.id)
    s.commit()
    assert counts["conflicts"] == 1 and counts["queued"] == 0
    clash = s.query(Branch).filter_by(name="b/c").one()
    assert clash.host is None and "already used" in clash.host_conflict


def test_disabled_branch_not_built(s, fake_github):
    p = _project(s)
    fake_github[:] = [BranchInfo("v18", "a" * 40)]
    sync.sync_project(s, p.id)
    s.commit()
    b = s.query(Branch).one()
    b.enabled = False
    s.commit()
    fake_github[:] = [BranchInfo("v18", "f" * 40)]
    counts = sync.sync_project(s, p.id)
    assert counts["queued"] == 0


def test_auto_rebuild_off_and_deleted_builds(s, fake_github):
    p = _project(s, auto_build_regex="^v18$")
    fake_github[:] = [BranchInfo("v18", "a" * 40)]
    sync.sync_project(s, p.id)
    s.commit()
    b = s.query(Branch).one()
    assert s.query(Build).count() == 1

    # Auto-rebuild off: a new commit is recorded but not built.
    b.auto_rebuild = False
    s.commit()
    fake_github[:] = [BranchInfo("v18", "b" * 40)]
    assert sync.sync_project(s, p.id)["queued"] == 0
    s.commit()
    assert b.last_commit_sha == "b" * 40 and s.query(Build).count() == 1

    # All builds deleted, auto-rebuild on: no rebuild until a new commit arrives.
    b.auto_rebuild = True
    s.query(Build).delete()
    s.commit()
    assert sync.sync_project(s, p.id)["queued"] == 0
    fake_github[:] = [BranchInfo("v18", "c" * 40)]
    assert sync.sync_project(s, p.id)["queued"] == 1


def test_sync_uses_project_account_and_names_it_on_error(s, monkeypatch):
    p = _project(s)
    partner = GithubCredential(name="Partner", username="odoo-partner", token_enc=encrypt("ptok"))
    p.credential = partner
    s.commit()
    seen = []

    def refuse(url, token):
        seen.append(token)
        raise github.GitHubError("GitHub rejected the token (401)")
    monkeypatch.setattr(sync.github, "list_branches", refuse)
    with pytest.raises(github.GitHubError) as e:
        sync.sync_project(s, p.id)
    assert seen == ["ptok"]
    assert "credential 'Partner' (odoo-partner)" in str(e.value) and "Git credentials page" in str(e.value)
    assert "ptok" not in str(e.value)


# ---------- combined previews ----------

@pytest.fixture
def heads(monkeypatch):
    """{(repo name, branch): sha}; a missing entry = the branch does not exist."""
    table: dict[tuple[str, str], str] = {}

    def remote_head(url, branch, token):
        return table.get((url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1], branch))
    monkeypatch.setattr(sync.gitmirror, "remote_head", remote_head)
    return table


def _combo(s):
    from previewer.models import COMBO_BRANCH, ProjectDependency
    p = _project(s, slug="hr-suite", kind="combo")
    p.repo_url = ""
    pay = ProjectDependency(project=p, repo_url="https://github.com/a/payroll_extension", module_name="payroll_zm")
    bank = ProjectDependency(project=p, repo_url="https://github.com/a/bank_extension.git",
                             module_name="bank_zm", position=1)
    b = Branch(project=p, name=COMBO_BRANCH, slug="combined", enabled=True)
    s.add_all([pay, bank, b])
    s.flush()
    b.dependency_branches = {str(pay.id): "v18-astro", str(bank.id): "main"}
    s.commit()
    return p, b


def test_combo_sync_builds_once_and_on_part_move(s, heads, fake_github):
    p, b = _combo(s)
    heads.update({("payroll_extension", "v18-astro"): "1" * 40, ("bank_extension", "main"): "2" * 40})
    fake_github.append(BranchInfo("should-not-be-listed", "9" * 40))
    counts = sync.sync_project(s, p.id)
    s.commit()
    assert counts["queued"] == 1 and counts["updated"] == 1
    assert b.host == "hr-suite.preview.localhost"
    assert b.last_commit_message == "payroll_zm@v18-astro 1111111 · bank_zm@main 2222222"
    assert b.last_commit_author == "combined" and len(b.last_commit_sha) == 40
    assert s.query(Branch).count() == 1  # GitHub branches are not listed for a combo
    first = b.last_commit_sha

    assert sync.sync_project(s, p.id)["queued"] == 0  # nothing moved
    s.commit()

    heads[("bank_extension", "main")] = "3" * 40
    counts = sync.sync_project(s, p.id)
    s.commit()
    assert counts["queued"] == 1 and b.last_commit_sha != first
    assert [x.status for x in s.query(Build).order_by(Build.id)] == ["removed", "queued"]


def test_combo_sync_missing_part_branch(s, heads):
    p, b = _combo(s)
    heads[("payroll_extension", "v18-astro")] = "1" * 40
    with pytest.raises(sync.SyncError, match="bank_zm: a/bank_extension has no branch 'main'"):
        sync.sync_project(s, p.id)
    s.rollback()
    assert s.query(Build).count() == 0
