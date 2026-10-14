"""Add bounded diagnostics without changing tenant policies or business state."""
from alembic import op
import sqlalchemy as sa
revision='0003_diagnostics'
down_revision='0002_approvals'

def upgrade():
    # 0001 uses current metadata on fresh databases; tolerate columns already present.
    columns={c['name'] for c in sa.inspect(op.get_bind()).get_columns('usage_events')}
    if 'detail' not in columns:op.add_column('usage_events',sa.Column('detail',sa.JSON(),server_default=sa.text("'{}'::json"),nullable=False))
    columns={c['name'] for c in sa.inspect(op.get_bind()).get_columns('outbox_events')}
    if 'last_error' not in columns:op.add_column('outbox_events',sa.Column('last_error',sa.String(40),server_default='',nullable=False))

def downgrade():
    # Old application code ignores these additive columns; evidence is retained.
    pass
