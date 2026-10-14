from alembic import op
import sqlalchemy as sa
revision='0009_service_mode'
down_revision='0008_document_knowledge'
def upgrade():
    if 'service_mode' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('company_settings')}:
        op.add_column('company_settings',sa.Column('service_mode',sa.String(30),nullable=False,server_default='sales_consulting'))
    op.alter_column('company_settings','service_mode',server_default='')
def downgrade():pass
