"""Encrypted notification security options, compatible with previous pilot inserts."""
from alembic import op
import sqlalchemy as sa
revision='0006_notification_signing'
down_revision='0005_channels'
def upgrade():
    if 'notification_options_cipher' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('company_settings')}:
        op.add_column('company_settings',sa.Column('notification_options_cipher',sa.Text(),nullable=False,server_default=''))
    op.alter_column('company_settings','notification_options_cipher',server_default='')
def downgrade():pass
