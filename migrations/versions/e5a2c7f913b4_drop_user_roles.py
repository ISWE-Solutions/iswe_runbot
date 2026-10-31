"""drop user roles

Revision ID: e5a2c7f913b4
Revises: b3c8e1f02a57
"""
from alembic import op
import sqlalchemy as sa

revision = 'e5a2c7f913b4'
down_revision = 'b3c8e1f02a57'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column('users', 'role')


def downgrade():
    op.add_column('users', sa.Column('role', sa.String(length=20), server_default='admin', nullable=False))
