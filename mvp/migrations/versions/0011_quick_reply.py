"""Reviewed exact-question replies bind to one authorized source version."""
from alembic import op
import sqlalchemy as sa
revision='0011_quick_reply'
down_revision='0010_evidence_leases'


def upgrade():
    names={c['name'] for c in sa.inspect(op.get_bind()).get_columns('knowledge')}
    if 'quick_reply' not in names:
        op.add_column('knowledge',sa.Column('quick_reply',sa.JSON(),nullable=False,server_default='{}'))
    else:op.alter_column('knowledge','quick_reply',server_default='{}')


def downgrade():
    raise RuntimeError('Preserve reviewed company replies; roll back source only.')
