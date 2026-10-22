"""Add explicit analytics annotations and question-level visitor feedback."""
from alembic import op
import sqlalchemy as sa
from mvp.models import AnalyticsRecord,AnswerFeedback

revision='0013_customer_analytics'
down_revision='0012_case_recommendations'

def upgrade():
    bind=op.get_bind()
    for model in (AnalyticsRecord,AnswerFeedback):
        table=model.__table__;table.create(bind,checkfirst=True)
        bind.execute(sa.text(f'ALTER TABLE {table.name} ENABLE ROW LEVEL SECURITY'))
        bind.execute(sa.text(f'ALTER TABLE {table.name} FORCE ROW LEVEL SECURITY'))
        exists=bind.execute(sa.text('SELECT 1 FROM pg_policies WHERE tablename=:table AND policyname=\'tenant_scope\''),{'table':table.name}).scalar()
        if not exists:bind.execute(sa.text(f"CREATE POLICY tenant_scope ON {table.name} USING (tenant_id=current_setting('app.tenant_id',true)) WITH CHECK (tenant_id=current_setting('app.tenant_id',true))"))
        bind.execute(sa.text(f'GRANT SELECT,INSERT,UPDATE,DELETE ON {table.name} TO salescare_app'))

def downgrade():
    raise RuntimeError('Preserve reviewed evidence and feedback; old code can ignore additive tables.')
