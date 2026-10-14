"""Add tenant channel instances and durable delivery; preserve existing website sessions."""
from alembic import op
import sqlalchemy as sa
from mvp.models import Base
revision='0005_channels'
down_revision='0004_diagnostics_indexes'
def upgrade():
    bind=op.get_bind()
    for table in ('auth_sessions','conversations'):
        if 'channel_id' not in {c['name'] for c in sa.inspect(bind).get_columns(table)}:
            op.add_column(table,sa.Column('channel_id',sa.String(40),nullable=True))
    if 'notification_options_cipher' not in {c['name'] for c in sa.inspect(bind).get_columns('company_settings')}:
        op.add_column('company_settings',sa.Column('notification_options_cipher',sa.Text(),nullable=False,server_default=''))
    op.alter_column('company_settings','notification_options_cipher',server_default='')
    for name in ('channels','external_threads','channel_receipts','channel_deliveries'):
        Base.metadata.tables[name].create(bind,checkfirst=True)
        op.execute(f'ALTER TABLE {name} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {name} FORCE ROW LEVEL SECURITY')
        exists=bind.scalar(sa.text('SELECT count(*) FROM pg_policies WHERE schemaname=\'public\' AND tablename=:name AND policyname=\'tenant_scope\''),{'name':name})
        if not exists:
            op.execute(f"CREATE POLICY tenant_scope ON {name} USING (tenant_id=current_setting('app.tenant_id',true)) WITH CHECK (tenant_id=current_setting('app.tenant_id',true))")
        op.execute(f'GRANT SELECT,INSERT,UPDATE,DELETE ON {name} TO salescare_app')
    op.execute('CREATE INDEX IF NOT EXISTS ix_channel_delivery_queue ON channel_deliveries (tenant_id,status,due_at)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_conversation_channel ON conversations (tenant_id,channel_id)')
def downgrade():
    raise RuntimeError('Channel records are durable; use source rollback without dropping tenant data.')
