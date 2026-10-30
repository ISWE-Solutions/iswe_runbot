"""drop odoo 16 and 17

Revision ID: 506dbda98091
Revises: 9a4d63dacb45
"""
from alembic import op
import sqlalchemy as sa


revision = '506dbda98091'
down_revision = '9a4d63dacb45'
branch_labels = None
depends_on = None


def upgrade():
    # Only when nothing uses them (projects/branches reference odoo_versions by FK).
    op.execute("""
        DELETE FROM odoo_versions v
        WHERE v.version IN ('16.0', '17.0')
          AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.default_odoo_version = v.version)
          AND NOT EXISTS (SELECT 1 FROM branches b WHERE b.odoo_version_override = v.version)
    """)


def downgrade():
    op.execute("""
        INSERT INTO odoo_versions (version, docker_image, demo_flag_on, demo_flag_off, enabled,
                                   enterprise_repo_url, enterprise_branch)
        VALUES ('16.0', 'odoo:16', '', '--without-demo=all', false, '', ''),
               ('17.0', 'odoo:17', '', '--without-demo=all', false, '', '')
        ON CONFLICT (version) DO NOTHING
    """)
