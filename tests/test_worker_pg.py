"""worker/pg.py against the test Postgres server: what a preview's own role can and cannot do.
Every role and database here is prefixed pvtest_ and dropped afterwards."""
import psycopg
import pytest

from previewer.config import get_settings
from previewer.worker import hardening, pg

pytestmark = pytest.mark.real_pg

PW = "pvtest-password-1"


def _drop_all():
    c = get_settings()
    with pg._connect() as conn:
        for (db,) in conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'pvtest%'").fetchall():
            conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        for (role,) in conn.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE 'pvtest%'").fetchall():
            for (db,) in conn.execute("SELECT datname FROM pg_database WHERE datallowconn").fetchall():
                with pg._connect(db) as dconn:
                    dconn.execute(f'DROP OWNED BY "{role}"')
            conn.execute(f'DROP ROLE IF EXISTS "{role}"')
    assert c.postgres_user  # the superuser itself is never touched


@pytest.fixture(autouse=True)
def clean_pg():
    _drop_all()
    yield
    _drop_all()


def login(role, db, password=PW):
    c = get_settings()
    return psycopg.connect(host=c.postgres_host, port=c.postgres_port, user=role, password=password,
                           dbname=db, autocommit=True, connect_timeout=5)


def test_build_role_owns_only_its_database():
    pg.ensure_role("pvtest_b1", PW)
    pg.ensure_role("pvtest_b2", PW)
    pg.create_empty_database("pvtest_db1", "pvtest_b1")
    pg.create_empty_database("pvtest_db2", "pvtest_b2")
    pg.protect_app_database()
    with login("pvtest_b1", "pvtest_db1") as conn:
        conn.execute("CREATE TABLE res_partner (id serial primary key, name text)")
        conn.execute("INSERT INTO res_partner (name) VALUES ('x')")
        assert conn.execute("SELECT extname FROM pg_extension WHERE extname = 'pg_trgm'").fetchone()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE DATABASE pvtest_evil")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE ROLE pvtest_evil")
    for other in ("pvtest_db2", get_settings().postgres_db):
        with pytest.raises(psycopg.OperationalError, match="permission denied"):
            login("pvtest_b1", other)
    attrs = pg._connect().execute("SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles "
                                  "WHERE rolname = 'pvtest_b1'").fetchone()
    assert attrs == (False, False, False)
    assert pg.database_owner("pvtest_db1") == "pvtest_b1"


def test_template_copy_belongs_to_the_build_role():
    pg.ensure_role("pvtest_t", PW)
    pg.create_template_database("pvtest_tpl", "pvtest_t")
    with login("pvtest_t", "pvtest_tpl") as conn:
        conn.execute("CREATE TABLE ir_module_module (id serial primary key, name text, state text)")
        conn.execute("CREATE VIEW v_mods AS SELECT name FROM ir_module_module")
        conn.execute("CREATE SEQUENCE ir_seq_1")
        conn.execute("CREATE FUNCTION f_one() RETURNS int LANGUAGE sql AS 'SELECT 1'")
        conn.execute("INSERT INTO ir_module_module (name, state) VALUES ('base', 'installed')")
    pg.seal_template("pvtest_tpl", "pvtest_t")
    with pytest.raises(psycopg.OperationalError):
        login("pvtest_t", "pvtest_tpl")

    pg.ensure_role("pvtest_b7", PW)
    pg.create_from_template("pvtest_copy", "pvtest_tpl", "pvtest_b7", template_owner="pvtest_t")
    with login("pvtest_b7", "pvtest_copy") as conn:
        conn.execute("INSERT INTO ir_module_module (name, state) VALUES ('payroll_zm', 'installed')")
        conn.execute("ALTER TABLE ir_module_module ADD COLUMN x int")  # owner-only
        conn.execute("SELECT nextval('ir_seq_1'), f_one()")
        owners = conn.execute("""
            SELECT DISTINCT pg_get_userbyid(relowner) FROM pg_class c JOIN pg_namespace n ON n.oid = relnamespace
            WHERE nspname = 'public'""").fetchall()
        assert owners == [("pvtest_b7",)]
    with pytest.raises(psycopg.OperationalError, match="permission denied"):
        login("pvtest_b7", "pvtest_tpl")
    assert pg.database_owner("pvtest_tpl") == get_settings().postgres_user
    assert pg.module_states("pvtest_copy", ["base", "payroll_zm"]) == {"base": "installed", "payroll_zm": "installed"}

    pg.drop_database("pvtest_copy")
    pg.drop_role("pvtest_b7")
    pg.drop_database("pvtest_tpl")
    pg.drop_role("pvtest_t")
    assert not pg.role_exists("pvtest_b7") and not pg.role_exists("pvtest_t")


def test_legacy_shared_role_is_split_per_build(s, monkeypatch, tmp_path):
    """Two previews made by the old shared role; afterwards each has its own role and the
    shared role cannot log in."""
    from previewer.models import Branch, Build, Project
    c = get_settings()
    monkeypatch.setattr(c, "odoo_db_user", "pvtest_shared")
    monkeypatch.setattr(pg, "build_role", lambda build_id: f"pvtest_b{build_id}")
    monkeypatch.setattr(hardening.docker_ops, "check_preview_network", lambda: None)
    monkeypatch.setattr(hardening.docker_ops, "get_container", lambda name: None)
    monkeypatch.setattr(hardening, "build_dir", lambda build_id: tmp_path / str(build_id))

    with pg._connect() as conn:
        conn.execute(f"CREATE ROLE pvtest_shared LOGIN CREATEDB PASSWORD '{PW}'")
    p = Project(name="P", slug="p", repo_url="https://github.com/a/p", default_odoo_version="18.0")
    s.add(p)
    s.flush()
    br = Branch(project=p, name="v18", slug="v18", host="p-v18.preview.localhost", last_commit_sha="a" * 40)
    s.add(br)
    s.flush()
    builds = []
    for i in (1, 2):
        b = Build(branch_id=br.id, commit_sha="a" * 40, odoo_version="18.0", status="running",
                  host=br.host, db_name=f"pvtest_legacy{i}")
        s.add(b)
        s.flush()
        builds.append(b)
        with pg._connect() as conn:
            conn.execute(f'CREATE DATABASE pvtest_legacy{i} OWNER pvtest_shared')
        with login("pvtest_shared", f"pvtest_legacy{i}") as conn:
            conn.execute("CREATE TABLE res_users (id serial primary key, login text)")
            conn.execute("CREATE VIEW v AS SELECT login FROM res_users")
        (tmp_path / str(b.id)).mkdir()
        (tmp_path / str(b.id) / "odoo.conf").write_text("[options]\ndb_user = pvtest_shared\ndb_password = x\n")
    with pg._connect() as conn:
        conn.execute("CREATE DATABASE pvtest_leftover OWNER pvtest_shared")
    s.commit()

    assert "secured 2 preview(s)" in hardening.secure_legacy()
    for i, b in enumerate(builds, 1):
        role = f"pvtest_b{b.id}"
        assert pg.database_owner(f"pvtest_legacy{i}") == role
        conf = (tmp_path / str(b.id) / "odoo.conf").read_text()
        assert f"db_user = {role}" in conf
        password = conf.split("db_password = ")[1].split("\n")[0]
        with login(role, f"pvtest_legacy{i}", password) as conn:
            conn.execute("INSERT INTO res_users (login) VALUES ('admin')")
            conn.execute("SELECT * FROM v")
    other_role = f"pvtest_b{builds[1].id}"
    pw2 = (tmp_path / str(builds[1].id) / "odoo.conf").read_text().split("db_password = ")[1].split("\n")[0]
    with pytest.raises(psycopg.OperationalError, match="permission denied"):
        login(other_role, "pvtest_legacy1", pw2)
    assert pg.database_owner("pvtest_leftover") == c.postgres_user
    with pytest.raises(psycopg.OperationalError):
        login("pvtest_shared", "pvtest_legacy1")
    assert pg._connect().execute("SELECT rolcanlogin, rolcreatedb FROM pg_roles WHERE rolname = 'pvtest_shared'"
                                 ).fetchone() == (False, False)
    assert hardening.secure_legacy() == "nothing to secure"
