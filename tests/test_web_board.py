from datetime import datetime, timedelta, timezone

from test_web_app import make_branch, make_project, user_client  # noqa: F401  (user_client is a fixture)

from previewer.models import Branch, Build

NOW = datetime.now(timezone.utc)


def branch(s, p, name, sticky=False, commit_age_h=1, **kw):
    b = make_branch(s, p, name=name, slug=name.replace("/", "-").replace(".", "-"))
    b.sticky = sticky
    b.last_commit_at = NOW - timedelta(hours=commit_age_h)
    for k, v in kw.items():
        setattr(b, k, v)
    s.commit()
    return b


def build(s, b, status, version="18.0", started=True, error=None, sha="b" * 40):
    bd = Build(branch_id=b.id, commit_sha=sha, odoo_version=version, status=status, host=b.host,
               started_at=NOW if started else None, error_summary=error)
    s.add(bd)
    s.commit()
    return bd


def row_order(html: str, names: list[str]) -> list[str]:
    found = [(html.find(f">{n}</a>"), n) for n in names]
    return [n for pos, n in sorted(found) if pos >= 0]


def test_board_rows_sticky_first_then_recent(s, user_client):
    p = make_project(s)
    branch(s, p, "feature-old", commit_age_h=48)
    branch(s, p, "v18", sticky=True)
    branch(s, p, "main", sticky=True)
    branch(s, p, "feature-new", commit_age_h=1)
    branch(s, p, "v19", sticky=True)
    html = user_client.get("/").text
    assert row_order(html, ["v19", "v18", "main", "feature-new", "feature-old"]) == \
        ["v19", "v18", "main", "feature-new", "feature-old"]


def test_board_filters(s, user_client):
    p = make_project(s)
    branch(s, p, "v18", sticky=True)
    branch(s, p, "feature-tax")
    branch(s, p, "gone", deleted_at=NOW)
    c = user_client
    sticky = c.get("/?show=sticky").text
    assert ">v18</a>" in sticky and ">feature-tax</a>" not in sticky
    dev = c.get("/?show=dev").text
    assert ">feature-tax</a>" in dev and ">v18</a>" not in dev
    search = c.get("/?q=TAX").text
    assert ">feature-tax</a>" in search and ">v18</a>" not in search
    assert ">gone</a>" not in c.get("/").text


def test_board_cards_skip_superseded_and_show_errors(s, user_client):
    p = make_project(s, edition="enterprise")
    b = branch(s, p, "v18", sticky=True)
    build(s, b, "running", sha="1111111" + "a" * 33)
    build(s, b, "removed", started=False, error="Superseded by a newer build", sha="2222222" + "a" * 33)
    build(s, b, "failed", version="19.0", sha="3333333" + "a" * 33,
          error="Odoo Enterprise 19.0 is not available: acme/enterprise has no 19.0 branch.")
    html = user_client.get("/").text
    assert "1111111" in html and "3333333" in html and "2222222" not in html
    assert "19.0<span class=\"sm:hidden\"> EE</span><span class=\"hidden sm:inline\"> · Enterprise" in html
    assert "has no 19.0 branch" in html
    assert 'href="http://payroll-v18.preview.localhost"' in html  # open arrow on the live build
    assert "Failed: 1" in html and "Running: 1" in html


def test_board_counters(s, user_client):
    p = make_project(s)
    build(s, branch(s, p, "a"), "queued", started=False)
    build(s, branch(s, p, "b"), "building")
    build(s, branch(s, p, "c"), "running")
    html = user_client.get("/").text
    assert "Queued: 1" in html and "Building: 1" in html and "Running: 1" in html
    assert 'hx-trigger="every 5s"' in html  # polls fast while something is active


def test_board_project_tab_is_remembered(s, user_client):
    p1 = make_project(s)
    p2 = make_project_named(s, "Bank", "bank")
    branch(s, p1, "payroll-only")
    make_branch(s, p2, name="bank-only", slug="bank-only")
    c = user_client
    first = c.get("/?project=bank").text
    assert ">bank-only</a>" in first and ">payroll-only</a>" not in first
    again = c.get("/").text
    assert ">bank-only</a>" in again and ">payroll-only</a>" not in again
    everything = c.get("/?project=all").text
    assert ">bank-only</a>" in everything and ">payroll-only</a>" in everything


def make_project_named(s, name, slug):
    from previewer.models import Project
    p = Project(name=name, slug=slug, repo_url=f"https://github.com/acme/{slug}",
                default_odoo_version="19.0")
    s.add(p)
    s.commit()
    return p


def test_star_toggle(s, user_client):
    p = make_project(s)
    b = branch(s, p, "feature-x")
    r = user_client.post(f"/branches/{b.id}/sticky", headers={"HX-Request": "true", "HX-Target": "board-rows"})
    assert r.status_code == 200 and 'id="board-rows"' in r.text
    s.expire_all()
    assert s.get(Branch, b.id).sticky is True


def test_friendly_link_uses_live_build_version(s, user_client):
    p = make_project(s)
    b = branch(s, p, "v18", sticky=True, odoo_version_override="19.0")
    build(s, b, "running", version="18.0")
    build(s, b, "failed", version="19.0", error="boom")
    c = user_client
    detail = c.get(f"/projects/{p.id}").text
    assert 'href="/18.0/payroll/v18"' in detail and "/19.0/payroll/v18" not in detail
    assert "running 18.0 · 19.0 failed" in detail
    page = c.get(f"/branches/{b.id}").text
    assert 'href="/18.0/payroll/v18"' in page and "running 18.0" in page
    r = c.get("/18.0/payroll/v18")
    assert r.status_code == 302 and r.headers["location"] == "http://payroll-v18.preview.localhost"


def test_projects_list_moved(s, user_client):
    make_project(s)
    c = user_client
    r = c.get("/projects")
    assert r.status_code == 200 and "Payroll" in r.text
