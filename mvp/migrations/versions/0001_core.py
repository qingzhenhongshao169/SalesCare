"""Normalized MVP schema and tenant row security."""
from alembic import op
from sqlalchemy import text
from mvp.models import Base, SCOPED_TABLES

revision = '0001_core'
down_revision = None

def upgrade():
    bind = op.get_bind()
    Base.metadata.create_all(bind)
    for table in SCOPED_TABLES:
        bind.execute(text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        bind.execute(text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
        bind.execute(text(f'''CREATE POLICY tenant_scope ON "{table}" USING
            (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK
            (tenant_id = current_setting('app.tenant_id', true))'''))
    bind.execute(text('GRANT USAGE ON SCHEMA public TO salescare_app'))
    bind.execute(text('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO salescare_app'))
    bind.execute(text('REVOKE UPDATE,DELETE ON audit_events FROM salescare_app'))
    bind.execute(text('REVOKE ALL ON alembic_version FROM salescare_app'))

def downgrade():
    raise RuntimeError('Destructive downgrade prohibited; restore the verified backup instead.')
