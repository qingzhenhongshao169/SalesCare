"""Database-enforced channel/session tenant linkage, additive to existing conversations."""
from alembic import op
import sqlalchemy as sa
revision='0007_channel_foreign_keys'
down_revision='0006_notification_signing'
def upgrade():
    for table in ('auth_sessions','conversations'):
        existing=sa.inspect(op.get_bind()).get_foreign_keys(table)
        if not any(f['referred_table']=='channels' and f['constrained_columns']==['tenant_id','channel_id'] for f in existing):
            op.create_foreign_key('fk_'+table+'_tenant_channel',table,'channels',['tenant_id','channel_id'],['tenant_id','id'])
def downgrade():pass
