"""Add document ingestion without changing existing conversation or knowledge rows."""
from alembic import op
import sqlalchemy as sa
from mvp.models import Base
revision='0008_document_knowledge'
down_revision='0007_channel_foreign_keys'

def upgrade():
    bind=op.get_bind()
    for name in ('kb_collections','kb_documents','kb_document_versions','kb_chunks','kb_jobs','kb_engine_configs'):
        Base.metadata.tables[name].create(bind,checkfirst=True)
        op.execute(f'ALTER TABLE {name} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {name} FORCE ROW LEVEL SECURITY')
        exists=bind.scalar(sa.text("SELECT count(*) FROM pg_policies WHERE schemaname='public' AND tablename=:name AND policyname='tenant_scope'"),{'name':name})
        if not exists:op.execute(f"CREATE POLICY tenant_scope ON {name} USING (tenant_id=current_setting('app.tenant_id',true)) WITH CHECK (tenant_id=current_setting('app.tenant_id',true))")
        op.execute(f'GRANT SELECT,INSERT,UPDATE,DELETE ON {name} TO salescare_app')
    op.execute('CREATE INDEX IF NOT EXISTS ix_kb_jobs_queue ON kb_jobs (tenant_id,status,lease_until)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_kb_chunk_version ON kb_chunks (tenant_id,version_id,position)')

def downgrade():
    raise RuntimeError('Preserve uploaded documents; roll back source without dropping company data.')
