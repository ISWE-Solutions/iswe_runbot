"""Tests run inside the app container against a throwaway database:

    docker compose exec app pytest                       # uses previewer_test
    docker compose exec -e TEST_DATABASE=previewer_test_x app pytest   # parallel runs

Never point TEST_DATABASE at the app's own database.
"""
import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from previewer import db
from previewer.config import get_settings

TABLES = "users, projects, branches, builds, jobs, settings, db_templates, scheduler_state"


@pytest.fixture(scope="session", autouse=True)
def test_database():
    cfg = get_settings()
    name = os.environ.get("TEST_DATABASE", cfg.test_database)
    assert name != cfg.postgres_db, "TEST_DATABASE must not be the app database"
    admin = create_engine(cfg._url(cfg.postgres_db), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = cfg._url(name)
    os.environ["ALEMBIC_DATABASE_URL"] = url
    alembic_cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    alembic_cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "migrations"))
    command.upgrade(alembic_cfg, "head")
    os.environ.pop("ALEMBIC_DATABASE_URL")
    db.configure(url)
    from previewer.cli import seed_versions
    seed_versions()
    yield url
    db.engine().dispose()
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture(autouse=True)
def clean_tables(test_database):
    yield
    with db.engine().begin() as c:
        c.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        c.execute(text("UPDATE odoo_versions SET enterprise_repo_url = '', enterprise_branch = '',"
                       " enterprise_credential_id = NULL"))
        # Not TRUNCATE ... CASCADE: that would also empty odoo_versions (FK to accounts)
        c.execute(text("DELETE FROM github_credentials"))
    from previewer.cli import seed_versions
    seed_versions()  # put back any version a test deleted


@pytest.fixture(autouse=True)
def no_github(monkeypatch):
    """Tests never reach GitHub; tests that need it monkeypatch github functions themselves."""
    from previewer import deps, github

    def offline(*a, **k):
        raise github.GitHubError("GitHub is offline in tests")

    monkeypatch.setattr(github, "_client", offline)
    deps._cache.clear()


@pytest.fixture
def s():
    session = db.SessionLocal()
    yield session
    session.close()
