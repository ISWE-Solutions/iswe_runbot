"""Superuser operations on the shared Postgres server (worker only)."""
import psycopg
from psycopg import sql

from ..config import get_settings


def _connect(dbname: str | None = None) -> psycopg.Connection:
    c = get_settings()
    return psycopg.connect(host=c.postgres_host, port=c.postgres_port, user=c.postgres_user,
                           password=c.postgres_password, dbname=dbname or c.postgres_db,
                           autocommit=True, connect_timeout=10)


def ensure_odoo_role() -> None:
    """LOGIN CREATEDB role for the previews (Odoo refuses to run as a superuser)."""
    c = get_settings()
    with _connect() as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (c.odoo_db_user,)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(sql.SQL(verb + " ROLE {} LOGIN CREATEDB NOSUPERUSER PASSWORD {}").format(
            sql.Identifier(c.odoo_db_user), sql.Literal(c.odoo_db_password)))


def drop_database(name: str) -> None:
    with _connect() as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def database_exists(name: str) -> bool:
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is not None


def module_states(db_name: str, modules: list[str]) -> dict[str, str]:
    """{module: state} for the given names; missing modules are absent from the dict."""
    with _connect(db_name) as conn:
        rows = conn.execute("SELECT name, state FROM ir_module_module WHERE name = ANY(%s)",
                            (modules,)).fetchall()
    return dict(rows)


def create_from_template(name: str, template: str, owner: str) -> None:
    """CREATE DATABASE <name> TEMPLATE <template> (the template must have no connections)."""
    with _connect() as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {} OWNER {}").format(
            sql.Identifier(name), sql.Identifier(template), sql.Identifier(owner)))


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
