import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from previewer import models  # noqa: F401  (registers tables)
from previewer.config import get_settings
from previewer.db import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

# ALEMBIC_DATABASE_URL lets tests migrate the test database explicitly.
url = os.environ.get("ALEMBIC_DATABASE_URL") or get_settings().database_url


def run_migrations_online():
    with create_engine(url).connect() as conn:
        context.configure(connection=conn, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
