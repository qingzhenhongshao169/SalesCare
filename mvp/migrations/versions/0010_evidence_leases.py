"""Add immutable build provenance and attempt fencing without rewriting company data."""
from alembic import op
import sqlalchemy as sa

revision = '0010_evidence_leases'
down_revision = '0009_service_mode'


def upgrade():
    # Initial migration creates from current metadata on fresh installations.
    for table, column in (
        ('kb_document_versions', sa.Column('build_manifest', sa.JSON(), nullable=False, server_default='{}')),
        ('kb_chunks', sa.Column('vector_digest', sa.String(64), nullable=False, server_default='')),
        ('kb_jobs', sa.Column('lease_token', sa.String(40), nullable=False, server_default='')),
        ('ai_jobs', sa.Column('lease_token', sa.String(40), nullable=False, server_default='')),
    ):
        names = {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}
        if column.name not in names:
            op.add_column(table, column)
        else:
            op.alter_column(table, column.name, server_default=column.server_default.arg)


def downgrade():
    raise RuntimeError('Keep provenance and company data; roll back application source only.')
