"""Compatibility defaults and tenant-scoped diagnostic/job lookup indexes."""
from alembic import op
import sqlalchemy as sa
revision='0004_diagnostics_indexes'
down_revision='0003_diagnostics'
def upgrade():
    op.alter_column('usage_events','detail',server_default=sa.text("'{}'::json"))
    op.alter_column('outbox_events','last_error',server_default='')
    op.execute("CREATE INDEX IF NOT EXISTS ix_usage_conversation_diagnostics ON usage_events (tenant_id, (detail->>'conversation_id'), created_at DESC)")
    op.execute('CREATE INDEX IF NOT EXISTS ix_ai_job_conversation_state ON ai_jobs (tenant_id, conversation_id, status)')
def downgrade():pass
