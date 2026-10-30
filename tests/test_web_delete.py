"""Deleting builds and the per-branch auto-rebuild switch."""
from test_web_app import as_role, make_branch, make_project  # noqa: F401  (as_role is a fixture)
from test_web_board import build

from previewer import jobs
from previewer.models import Branch, Build, Job


def _ids(s, branch_id):
    s.expire_all()
    return [b.id for b in s.query(Build).filter_by(branch_id=branch_id).order_by(Build.id)]


def test_delete_queued_cancels_job(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    bd = jobs.queue_build(s, b)
    s.commit()
    r = as_role("developer").post(f"/builds/{bd.id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert _ids(s, b.id) == []
    job = s.query(Job).filter_by(type="build").one()
    assert job.status == "cancelled"
    assert s.query(Job).filter_by(type="delete").count() == 0


def test_delete_building_refused(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    bd = build(s, b, "building")
    c = as_role("developer")
    c.post(f"/builds/{bd.id}/delete", headers={"referer": f"/branches/{b.id}"})
    assert _ids(s, b.id) == [bd.id]
    assert "still building" in c.get(f"/branches/{b.id}").text


def test_delete_live_enqueues_purge_and_flashes(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    bd = build(s, b, "running")
    bd.container_id, bd.db_name = "ctr123", "payroll_v18_aaaaaaa_b1"
    s.commit()
    bid = bd.id
    c = as_role("developer")
    r = c.post(f"/builds/{bid}/delete", follow_redirects=False)
    assert r.headers["location"] == f"/branches/{b.id}"
    assert _ids(s, b.id) == []
    job = s.query(Job).filter_by(type="delete").one()
    assert job.payload == {"build_id": bid, "container_id": "ctr123",
                           "db_name": "payroll_v18_aaaaaaa_b1", "purge_log": True}
    assert job.branch_id == b.id and job.build_id is None
    page = c.get(f"/branches/{b.id}").text
    assert "preview is gone" in page and "until the next new commit" in page


def test_delete_live_with_auto_rebuild_off_says_rebuild(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    b.auto_rebuild = False
    bd = build(s, b, "stopped")
    c = as_role("developer")
    c.post(f"/builds/{bd.id}/delete")
    assert "until you press Rebuild" in c.get(f"/branches/{b.id}").text


def test_delete_failed_from_board_rerenders_rows(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    keep = build(s, b, "running", sha="1111111" + "a" * 33)
    gone = build(s, b, "failed", sha="2222222" + "a" * 33, error="boom")
    r = as_role("developer").post(f"/builds/{gone.id}/delete?project=all",
                                  headers={"HX-Request": "true", "HX-Target": "board-rows"})
    assert r.status_code == 200 and 'id="board-rows"' in r.text
    assert "1111111" in r.text and "2222222" not in r.text
    assert _ids(s, b.id) == [keep.id]
    assert s.query(Job).filter_by(type="delete").one().payload["purge_log"] is True


def test_tester_cannot_delete_or_toggle(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    bd = build(s, b, "failed")
    c = as_role("tester")
    assert c.post(f"/builds/{bd.id}/delete").status_code == 403
    assert c.post(f"/branches/{b.id}/builds/delete", data={"all_failed": "1"}).status_code == 403
    assert c.post(f"/branches/{b.id}/auto-rebuild").status_code == 403
    assert _ids(s, b.id) == [bd.id]
    s.refresh(b)
    assert b.auto_rebuild
    html = c.get(f"/branches/{b.id}").text + c.get("/").text
    assert f"/builds/{bd.id}/delete" not in html and "Delete selected" not in html


def test_bulk_delete_and_delete_all_failed(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    f1, f2 = build(s, b, "failed"), build(s, b, "failed")
    removed, running, busy = build(s, b, "removed"), build(s, b, "running"), build(s, b, "building")
    c = as_role("developer")
    c.post(f"/branches/{b.id}/builds/delete", data={"ids": [str(removed.id), str(busy.id)]})
    assert _ids(s, b.id) == [f1.id, f2.id, running.id, busy.id]
    assert "Still building, not deleted" in c.get(f"/branches/{b.id}").text
    c.post(f"/branches/{b.id}/builds/delete", data={"all_failed": "1"})
    assert _ids(s, b.id) == [running.id, busy.id]
    assert s.query(Job).filter_by(type="delete").count() == 3


def test_auto_rebuild_toggle_and_settings(s, as_role):
    p = make_project(s)
    b = make_branch(s, p)
    c = as_role("developer")
    c.post(f"/branches/{b.id}/auto-rebuild")
    s.refresh(b)
    assert not b.auto_rebuild
    c.post(f"/branches/{b.id}/auto-rebuild")
    s.refresh(b)
    assert b.auto_rebuild
    form = c.get(f"/branches/{b.id}/settings").text
    assert 'name="auto_rebuild"' in form and "checked" in form
    c.post(f"/branches/{b.id}/settings", data={"demo_data_override": "default"})
    s.refresh(b)
    assert not b.auto_rebuild and s.query(Job).filter_by(type="build").count() == 0


def test_bulk_pause_resume(s, as_role):
    p = make_project(s)
    b1 = make_branch(s, p, name="v18", slug="v18")
    b2 = make_branch(s, p, name="v19", slug="v19")
    c = as_role("developer")
    c.post(f"/projects/{p.id}/branches/bulk", data={"ids": [str(b1.id), str(b2.id)], "action": "pause"})
    s.expire_all()
    assert not s.get(Branch, b1.id).auto_rebuild and not s.get(Branch, b2.id).auto_rebuild
    c.post(f"/projects/{p.id}/branches/bulk", data={"ids": [str(b2.id)], "action": "resume"})
    s.expire_all()
    assert not s.get(Branch, b1.id).auto_rebuild and s.get(Branch, b2.id).auto_rebuild
    assert s.query(Job).count() == 0


def test_board_shows_paused_and_unbuilt_commit(s, as_role):
    p = make_project(s)
    b = make_branch(s, p, sha="c" * 40)
    build(s, b, "running", sha="b" * 40)
    c = as_role("tester")
    assert "paused" not in c.get("/").text
    b.auto_rebuild = False
    s.commit()
    html = c.get("/").text
    assert "paused" in html and "new commit <code>ccccccc</code> not built" in html
    assert "new commit ccccccc not built" in c.get(f"/projects/{p.id}?tab=branches&sel=all").text
    assert "auto-rebuild paused" in c.get(f"/branches/{b.id}").text
