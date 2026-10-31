"""Superuser operations on the shared Postgres server (worker only).

Every build gets its own login role (`pv_b<build id>`) that owns only its own database, and
every database template its own role (`pvt_<key hash>`, login only while the template is being
made). No preview role can create databases or connect to any database but its own, so branch
code in one preview cannot read or change another preview, a template, or the app database.
"""
import logging

import psycopg
from psycopg import sql

from ..config import get_settings

log = logging.getLogger(__name__)


def _connect(dbname: str | None = None) -> psycopg.Connection:
    c = get_settings()
    return psycopg.connect(host=c.postgres_host, port=c.postgres_port, user=c.postgres_user,
                           password=c.postgres_password, dbname=dbname or c.postgres_db,
                           autocommit=True, connect_timeout=10)


# --- role names ----------------------------------------------------------------------------

def build_role(build_id: int) -> str:
    return f"pv_b{build_id}"


def template_role(key_hash: str) -> str:
    return f"pvt_{key_hash}"


# --- roles ---------------------------------------------------------------------------------

def ensure_role(name: str, password: str, *, conn_limit: int | None = None) -> None:
    """Create (or reset) a preview login role: no superuser, no CREATEDB, no CREATEROLE."""
    limit = get_settings().odoo_db_maxconn + 4 if conn_limit is None else conn_limit
    with _connect() as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(sql.SQL(verb + " ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                             "NOREPLICATION CONNECTION LIMIT {} PASSWORD {}").format(
            sql.Identifier(name), sql.Literal(int(limit)), sql.Literal(password)))


def disable_login(name: str) -> None:
    with _connect() as conn:
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone():
            conn.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(name)))


def drop_role(name: str) -> None:
    """Drop a preview role once its databases are gone. Anything it still owns elsewhere is
    handed to the app superuser first, so a leftover never blocks the drop."""
    c = get_settings()
    with _connect() as conn:
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone():
            return
        for (db,) in conn.execute(
                "SELECT datname FROM pg_database WHERE datdba = (SELECT oid FROM pg_roles "
                "WHERE rolname = %s)", (name,)).fetchall():
            conn.execute(sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                sql.Identifier(db), sql.Identifier(c.postgres_user)))
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


def role_exists(name: str) -> bool:
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone() is not None


def lock_shared_role() -> None:
    """The old shared preview role (ODOO_DB_USER): never CREATEDB again; NOLOGIN once no
    database still depends on it (see worker.security.secure_legacy_builds)."""
    c = get_settings()
    with _connect() as conn:
        row = conn.execute("SELECT oid FROM pg_roles WHERE rolname = %s", (c.odoo_db_user,)).fetchone()
        if not row:
            return
        conn.execute(sql.SQL("ALTER ROLE {} NOCREATEDB NOCREATEROLE NOSUPERUSER").format(
            sql.Identifier(c.odoo_db_user)))
        owns = conn.execute("SELECT count(*) FROM pg_database WHERE datdba = %s", (row[0],)).fetchone()[0]
        if not owns:
            conn.execute(sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(c.odoo_db_user)))


def protect_app_database() -> None:
    """Only the app superuser may connect to the app database (default: every role can)."""
    c = get_settings()
    with _connect() as conn:
        conn.execute(sql.SQL("REVOKE CONNECT, TEMPORARY ON DATABASE {} FROM PUBLIC").format(
            sql.Identifier(c.postgres_db)))


# --- databases -----------------------------------------------------------------------------

def _private(conn: psycopg.Connection, name: str) -> None:
    conn.execute(sql.SQL("REVOKE CONNECT, TEMPORARY ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name)))


def create_empty_database(name: str, owner: str) -> None:
    """An empty database for Odoo to initialise (what Odoo's own auto-create would make),
    owned by `owner`, which then needs no CREATEDB. Only the owner may connect."""
    with _connect() as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} OWNER {} ENCODING 'UTF8' LC_COLLATE 'C' "
                             "TEMPLATE template0").format(sql.Identifier(name), sql.Identifier(owner)))
        _private(conn, name)
    with _connect(name) as conn:
        # Odoo creates pg_trgm when it makes the database itself; it is a trusted extension.
        try:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        except psycopg.Error:
            log.warning("could not create pg_trgm in %s (Odoo works without it)", name)


def create_template_database(name: str, maker: str) -> None:
    """A template database: owned by the app superuser (so handing its objects to a build role
    later never touches the template itself), with `maker` allowed to create Odoo's tables."""
    c = get_settings()
    create_empty_database(name, c.postgres_user)
    with _connect() as conn:
        conn.execute(sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(
            sql.Identifier(name), sql.Identifier(maker)))
    with _connect(name) as conn:
        conn.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(sql.Identifier(maker)))


def seal_template(name: str, maker: str) -> None:
    """After the template is made: its maker can no longer log in or connect."""
    with _connect() as conn:
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(
            sql.Identifier(name), sql.Identifier(maker)))
    disable_login(maker)


def drop_database(name: str) -> None:
    with _connect() as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def database_exists(name: str) -> bool:
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is not None


def database_owner(name: str) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s",
                           (name,)).fetchone()
    return row[0] if row else None


def databases_owned_by(role: str) -> list[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT datname FROM pg_database WHERE datdba = (SELECT oid FROM pg_roles "
                            "WHERE rolname = %s) ORDER BY datname", (role,)).fetchall()
    return [r[0] for r in rows]


def set_database_owner(name: str, owner: str) -> None:
    with _connect() as conn:
        conn.execute(sql.SQL("ALTER DATABASE {} OWNER TO {}").format(sql.Identifier(name), sql.Identifier(owner)))
        _private(conn, name)


def hand_over(db_name: str, from_role: str, to_role: str) -> None:
    """Inside one database: everything `from_role` owns now belongs to `to_role`, and any
    privilege `from_role` held there is revoked. `from_role` must own no database (REASSIGN
    also moves shared objects), which holds for template roles and is ensured for the legacy
    shared role before this is called."""
    with _connect(db_name) as conn:
        conn.execute(sql.SQL("REASSIGN OWNED BY {} TO {}").format(
            sql.Identifier(from_role), sql.Identifier(to_role)))
        conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(from_role)))


def module_states(db_name: str, modules: list[str]) -> dict[str, str]:
    """{module: state} for the given names; missing modules are absent from the dict."""
    with _connect(db_name) as conn:
        rows = conn.execute("SELECT name, state FROM ir_module_module WHERE name = ANY(%s)",
                            (modules,)).fetchall()
    return dict(rows)


def create_from_template(name: str, template: str, owner: str, template_owner: str | None = None) -> None:
    """CREATE DATABASE <name> TEMPLATE <template> (the template must have no connections),
    then hand the template role's tables to `owner`, so the copy is fully `owner`'s."""
    with _connect() as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {} OWNER {}").format(
            sql.Identifier(name), sql.Identifier(template), sql.Identifier(owner)))
        _private(conn, name)
    if template_owner and template_owner != owner:
        hand_over(name, template_owner, owner)


def reset_database_identity(name: str) -> None:
    """A copied database must not share the template's uuid/secret, and its Enterprise
    expiry counts from now."""
    with _connect(name) as conn:
        conn.execute("""
            UPDATE ir_config_parameter SET value = CASE key
                WHEN 'database.uuid' THEN gen_random_uuid()::text
                WHEN 'database.secret' THEN gen_random_uuid()::text
                WHEN 'database.create_date' THEN to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
                WHEN 'database.expiration_date' THEN
                    to_char((now() + interval '30 days') AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
                ELSE value END
            WHERE key IN ('database.uuid', 'database.secret', 'database.create_date',
                          'database.expiration_date')""")


def database_size(name: str) -> int:
    with _connect() as conn:
        row = conn.execute("SELECT pg_database_size(%s)", (name,)).fetchone()
    return int(row[0]) if row else 0


def max_connections() -> int | None:
    try:
        with _connect() as conn:
            return int(conn.execute("SHOW max_connections").fetchone()[0])
    except psycopg.Error:
        return None
