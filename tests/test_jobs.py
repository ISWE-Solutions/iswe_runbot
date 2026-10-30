from previewer import jobs
from previewer.models import Branch, Build, Job, Project


def _branch(s):
    p = Project(name="Payroll", slug="payroll", repo_url="https://github.com/a/payroll_extension",
                default_odoo_version="18.0")
    b = Branch(project=p, name="v18", slug="v18", host="payroll-v18.preview.localhost",
               last_commit_sha="a" * 40)
    s.add_all([p, b])
    s.commit()
    return b


def test_queue_build_supersedes_queued(s):
    b = _branch(s)
    first = jobs.queue_build(s, b)
    second = jobs.queue_build(s, b)
    s.commit()
    assert s.get(Build, first.id).status == "removed"
    assert s.get(Build, second.id).status == "queued"
    statuses = sorted(j.status for j in s.query(Job).all())
    assert statuses == ["cancelled", "queued"]


def test_claim_is_exclusive(s, test_database):
    from previewer import db
    b = _branch(s)
    jobs.queue_build(s, b)
    s.commit()
    s1, s2 = db.SessionLocal(), db.SessionLocal()
    got = jobs.claim(s1, ("build",))
    assert got is not None and got.status == "running" and got.attempts == 1
    assert jobs.claim(s2, ("build",)) is None
    jobs.finish(s1, got)
    assert s1.get(Job, got.id).status == "done"
    s1.close(), s2.close()
