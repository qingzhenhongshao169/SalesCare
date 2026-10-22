from contextlib import contextmanager
from functools import lru_cache
from sqlalchemy import create_engine, select, text, event
from sqlalchemy.orm import Session
from .config import settings


@event.listens_for(Session, 'after_flush')
def notify_committed_work(session, _context):
    # PostgreSQL delivers NOTIFY only on successful commit, including messages
    # inserted by channel adapters. A rollback cannot wake a nonexistent task.
    from .models import AIJob, Message, Conversation
    from .realtime import JOB_CHANNEL, MESSAGE_CHANNEL, conversation_key
    if any(isinstance(row, AIJob) for row in session.new):
        session.execute(text('SELECT pg_notify(:channel, :payload)'), {'channel':JOB_CHANNEL, 'payload':''})
    new_conversations=[row for row in session.new if isinstance(row,Conversation)]
    if new_conversations and settings()['mode']=='test':
        from .models import AnalyticsRecord
        # Explicit synthetic defaults belong only to isolated QA environments.
        for row in new_conversations:
            if not any(isinstance(x,AnalyticsRecord) and x.tenant_id==row.tenant_id and x.id==row.id for x in session.new):
                session.add(AnalyticsRecord(tenant_id=row.tenant_id,id=row.id,data_kind='test'))
    conversations = {(row.tenant_id, row.conversation_id) for row in session.new if isinstance(row, Message)}
    conversations.update((row.tenant_id, row.id) for row in session.dirty if isinstance(row, Conversation))
    for tenant, conversation in conversations:
        session.execute(text('SELECT pg_notify(:channel, :payload)'),
                        {'channel':MESSAGE_CHANNEL, 'payload':conversation_key(tenant, conversation)})


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
