"""Human review evidence for automated high-risk knowledge candidates."""
from alembic import op
import sqlalchemy as sa
revision='0002_approvals'
down_revision='0001_core'

def upgrade():
    if 'approvals' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('knowledge')}:
        op.add_column('knowledge',sa.Column('approvals',sa.JSON(),server_default=sa.text("'[]'::json"),nullable=False))

def downgrade():raise RuntimeError('Restore a verified backup instead of removing approval evidence.')
