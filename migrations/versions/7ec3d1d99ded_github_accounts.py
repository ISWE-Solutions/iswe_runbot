"""github accounts

Revision ID: 7ec3d1d99ded
Revises: 13f75e3c4f31
"""
from alembic import op
import sqlalchemy as sa


revision = '7ec3d1d99ded'
down_revision = '13f75e3c4f31'
branch_labels = None
depends_on = None


def _accounts_from_tokens(conn):
    """Turn the per-project / per-dependency tokens into GitHub accounts. The token that
    matches GITHUB_TOKEN from .env (or else the first one found) becomes the default."""
    from previewer.config import get_settings
    from previewer.security import decrypt, encrypt

    cfg = get_settings()
    projects = conn.execute(sa.text("SELECT id, name, access_token_enc FROM projects ORDER BY id")).fetchall()
    deps = conn.execute(sa.text(
        "SELECT d.id, d.project_id, d.access_token_enc, d.repo_url FROM project_dependencies d ORDER BY d.id")).fetchall()
    tokens: dict[str, int] = {}

    def account(token: str, name: str, username: str = "", default: bool = False) -> int:
        if token in tokens:
            return tokens[token]
        cid = conn.execute(sa.text(
            "INSERT INTO github_credentials (name, username, token_enc, is_default, created_at)"
            " VALUES (:n, :u, :t, :d, now()) RETURNING id"),
            {"n": name, "u": username, "t": encrypt(token), "d": default}).scalar()
        tokens[token] = cid
        return cid

    plain = {pid: decrypt(enc) for pid, _, enc in projects if enc}
    if not plain and not any(enc for _, _, enc, _ in deps):
        return  # fresh install: `previewer.cli seed` creates the default account from .env
    env_token = cfg.github_token
    if env_token:
        default_token = env_token
    else:
        default_token = next(iter(plain.values()), None)
    if default_token:
        account(default_token, "Default", cfg.github_username if default_token == env_token else "",
                default=True)
    for pid, name, _ in projects:
        token = plain.get(pid)
        if token and token != default_token:
            cid = account(token, f"{name} token")
            conn.execute(sa.text("UPDATE projects SET credential_id = :c WHERE id = :p"), {"c": cid, "p": pid})
    for did, pid, enc, url in deps:
        if not enc:
            continue
        token = decrypt(enc)
        if token == plain.get(pid, default_token):
            continue  # same as the project's account: leave NULL
        cid = account(token, f"{url.rstrip('/').removesuffix('.git').rsplit('/', 1)[-1]} token")
        conn.execute(sa.text("UPDATE project_dependencies SET credential_id = :c WHERE id = :d"),
                     {"c": cid, "d": did})


def upgrade():
    op.create_table('github_credentials',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('username', sa.String(length=100), nullable=False),
    sa.Column('token_enc', sa.Text(), nullable=False),
    sa.Column('is_default', sa.Boolean(), nullable=False),
    sa.Column('checked_login', sa.String(length=100), nullable=True),
    sa.Column('checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('check_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.add_column('odoo_versions', sa.Column('enterprise_repo_url', sa.String(length=500), server_default='', nullable=False))
    op.add_column('odoo_versions', sa.Column('enterprise_branch', sa.String(length=100), server_default='', nullable=False))
    op.add_column('odoo_versions', sa.Column('enterprise_credential_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_odoo_versions_enterprise_credential', 'odoo_versions', 'github_credentials',
                          ['enterprise_credential_id'], ['id'], ondelete='SET NULL')
    op.add_column('project_dependencies', sa.Column('credential_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_project_dependencies_credential', 'project_dependencies', 'github_credentials',
                          ['credential_id'], ['id'], ondelete='SET NULL')
    op.add_column('projects', sa.Column('credential_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_projects_credential', 'projects', 'github_credentials',
                          ['credential_id'], ['id'], ondelete='SET NULL')
    _accounts_from_tokens(op.get_bind())
    op.drop_column('project_dependencies', 'access_token_enc')
    op.drop_column('projects', 'access_token_enc')


def downgrade():
    op.add_column('projects', sa.Column('access_token_enc', sa.Text(), server_default='', nullable=False))
    op.add_column('project_dependencies', sa.Column('access_token_enc', sa.Text(), server_default='', nullable=False))
    conn = op.get_bind()
    conn.execute(sa.text(
        "UPDATE projects p SET access_token_enc = c.token_enc FROM github_credentials c"
        " WHERE c.id = COALESCE(p.credential_id, (SELECT id FROM github_credentials WHERE is_default LIMIT 1))"))
    conn.execute(sa.text(
        "UPDATE project_dependencies d SET access_token_enc = c.token_enc FROM github_credentials c"
        " WHERE c.id = d.credential_id"))
    op.drop_constraint('fk_projects_credential', 'projects', type_='foreignkey')
    op.drop_column('projects', 'credential_id')
    op.drop_constraint('fk_project_dependencies_credential', 'project_dependencies', type_='foreignkey')
    op.drop_column('project_dependencies', 'credential_id')
    op.drop_constraint('fk_odoo_versions_enterprise_credential', 'odoo_versions', type_='foreignkey')
    op.drop_column('odoo_versions', 'enterprise_credential_id')
    op.drop_column('odoo_versions', 'enterprise_branch')
    op.drop_column('odoo_versions', 'enterprise_repo_url')
    op.drop_table('github_credentials')
