"""Add reviewed source-bound case cards without changing existing facts or prices."""
from alembic import op
import sqlalchemy as sa

revision = '0012_case_recommendations'
down_revision = '0011_quick_reply'


def upgrade():
    for table, column, default in (
        ('knowledge', 'case_recommendation', '{}'),
        ('messages', 'case_recommendations', '[]'),
        ('company_settings', 'case_recommendation_policy', '{}'),
    ):
        names = {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}
        if column not in names:
            op.add_column(table, sa.Column(column, sa.JSON(), nullable=False, server_default=default))
        else:
            op.alter_column(table, column, server_default=default)


def downgrade():
    raise RuntimeError('Preserve case source reviews and message history; disable the policy to roll back.')
