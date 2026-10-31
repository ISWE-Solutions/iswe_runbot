"""encrypt webhook secrets

Revision ID: b3c8e1f02a57
Revises: d9f0ac7d4e23
"""
from alembic import op
import sqlalchemy as sa

from previewer.security import decrypt, encrypt

revision = 'b3c8e1f02a57'
down_revision = 'd9f0ac7d4e23'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('projects', sa.Column('webhook_secret_enc', sa.String(length=255), server_default='',
                                        nullable=False))
    conn = op.get_bind()
    for pid, secret in conn.execute(sa.text("SELECT id, webhook_secret FROM projects WHERE webhook_secret <> ''")).all():
        conn.execute(sa.text("UPDATE projects SET webhook_secret_enc = :v WHERE id = :id"),
                     {"v": encrypt(secret), "id": pid})
    op.drop_column('projects', 'webhook_secret')


def downgrade():
    op.add_column('projects', sa.Column('webhook_secret', sa.String(length=64), server_default='',
                                        nullable=False))
    conn = op.get_bind()
    for pid, enc in conn.execute(sa.text("SELECT id, webhook_secret_enc FROM projects WHERE webhook_secret_enc <> ''")).all():
        conn.execute(sa.text("UPDATE projects SET webhook_secret = :v WHERE id = :id"),
                     {"v": decrypt(enc), "id": pid})
    op.drop_column('projects', 'webhook_secret_enc')
