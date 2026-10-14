from alembic import context
from sqlalchemy import create_engine
from mvp.config import settings
from mvp.models import Base

config = settings()
engine = create_engine(config['admin_database_url'], hide_parameters=True)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
