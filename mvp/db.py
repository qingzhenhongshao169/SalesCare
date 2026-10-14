from contextlib import contextmanager
from functools import lru_cache
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from .config import settings


@lru_cache
def engine():
    return create_engine(settings()['database_url'], pool_pre_ping=True, pool_size=12, max_overflow=8,
                         hide_parameters=True, connect_args={'connect_timeout':5})

@contextmanager
def transaction(tenant_id=None):
    with Session(engine(), expire_on_commit=False) as session, session.begin():
        if tenant_id:
            session.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {'tenant':tenant_id})
        yield session


def rows(session, model, tenant_id):
    return select(model).where(model.tenant_id == tenant_id)


def locked(session, model, tenant_id, identity):
    return session.scalar(rows(session, model, tenant_id).where(model.id == identity).with_for_update())


def database_time(session):
    return float(session.scalar(text('SELECT extract(epoch FROM clock_timestamp())')))


def serialization_lock(session, tenant_id, key):
    session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'), {'key':tenant_id + ':' + key})
