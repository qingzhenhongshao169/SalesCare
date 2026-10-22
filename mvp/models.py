import time
import uuid
from sqlalchemy import String, Float, Integer, Boolean, Text, JSON, ForeignKey, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid(): return uuid.uuid4().hex
def stamp(): return time.time()

class Base(DeclarativeBase): pass

class Tenant(Base):
    __tablename__ = 'tenants'
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(default=True)

class AuthSession(Base):
    __tablename__ = 'auth_sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey('tenants.id'))
    identity: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(20))
    csrf: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[float] = mapped_column(Float)
    revoked: Mapped[bool] = mapped_column(default=False)
    channel_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    __table_args__ = (ForeignKeyConstraint(['tenant_id','channel_id'],['channels.tenant_id','channels.id']),)

class LoginThrottle(Base):
    __tablename__ = 'login_throttles'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(default=0)
    until: Mapped[float] = mapped_column(Float)

class Scoped:
    tenant_id: Mapped[str] = mapped_column(String(40), ForeignKey('tenants.id'), primary_key=True)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    created_at: Mapped[float] = mapped_column(Float, default=stamp)

class Member(Scoped, Base):
    __tablename__ = 'members'
    username: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(Text)
    roles: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(default=True)
    skills: Mapped[list] = mapped_column(JSON, default=list)
    capacity: Mapped[int] = mapped_column(default=5)
    available: Mapped[bool] = mapped_column(default=False)
    heartbeat_at: Mapped[float] = mapped_column(Float, default=0)
    __table_args__ = (UniqueConstraint('tenant_id','username'),)

class Invitation(Scoped, Base):
    __tablename__ = 'invitations'
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    username: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(120))
    roles: Mapped[list] = mapped_column(JSON)
    expires_at: Mapped[float] = mapped_column(Float)
    used: Mapped[bool] = mapped_column(default=False)

class CompanySettings(Scoped, Base):
    __tablename__ = 'company_settings'
    tagline: Mapped[str] = mapped_column(String(300), default='让每一次咨询，都有好的回应。')
    service_mode: Mapped[str] = mapped_column(String(30),default='sales_consulting')
    timezone: Mapped[str] = mapped_column(String(80), default='Asia/Shanghai')
    weekdays: Mapped[list] = mapped_column(JSON, default=lambda:[0,1,2,3,4])
    opens: Mapped[str] = mapped_column(String(5), default='09:00')
    closes: Mapped[str] = mapped_column(String(5), default='18:00')
    required_skill: Mapped[str] = mapped_column(String(120), default='数字营销与开发咨询')
    allowed_origins: Mapped[list] = mapped_column(JSON, default=list)
    source_hosts: Mapped[list] = mapped_column(JSON, default=lambda:['www.biaofun.com','biaofun.com'])
    published: Mapped[bool] = mapped_column(default=False)
    daily_ai_limit: Mapped[int] = mapped_column(default=500)
    daily_token_limit: Mapped[int] = mapped_column(default=500000)
    ai_concurrency: Mapped[int] = mapped_column(default=4)
    retention_days: Mapped[int] = mapped_column(default=90)
    notification_cipher: Mapped[str] = mapped_column(Text, default='')
    notification_kind: Mapped[str] = mapped_column(String(30), default='none')
    notification_options_cipher: Mapped[str] = mapped_column(Text, default='')
    case_recommendation_policy: Mapped[dict] = mapped_column(JSON, default=dict)

class Conversation(Scoped, Base):
    __tablename__ = 'conversations'
    visitor_id: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(120), default='新访客')
    state: Mapped[str] = mapped_column(String(30), default='AI_ACTIVE')
    channel: Mapped[str] = mapped_column(String(20), default='web')
    channel_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    sequence: Mapped[int] = mapped_column(default=0)
    __table_args__ = (ForeignKeyConstraint(['tenant_id','channel_id'],['channels.tenant_id','channels.id']),)

class Message(Scoped, Base):
    __tablename__ = 'messages'
    conversation_id: Mapped[str] = mapped_column(String(40), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    client_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    role: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    actor_name: Mapped[str] = mapped_column(String(120), default='')
    text: Mapped[str] = mapped_column(Text)
    citations: Mapped[list] = mapped_column(JSON, default=list)
    case_recommendations: Mapped[list] = mapped_column(JSON, default=list)
    received_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    __table_args__ = (ForeignKeyConstraint(['tenant_id','conversation_id'],['conversations.tenant_id','conversations.id']),
                      UniqueConstraint('tenant_id','conversation_id','client_id'),
                      UniqueConstraint('tenant_id','conversation_id','sequence'))

class Lead(Scoped, Base):
    __tablename__ = 'leads'
    conversation_id: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(120))
    phone_cipher: Mapped[str] = mapped_column(Text, default='')
    consent_at: Mapped[float] = mapped_column(Float)
    withdrawn_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    consent_text: Mapped[str] = mapped_column(Text)
    __table_args__ = (ForeignKeyConstraint(['tenant_id','conversation_id'],['conversations.tenant_id','conversations.id']),
                      UniqueConstraint('tenant_id','conversation_id'))

class Task(Scoped, Base):
    __tablename__ = 'tasks'
    conversation_id: Mapped[str] = mapped_column(String(40))
    owner_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    skill: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(25), default='pending')
    version: Mapped[int] = mapped_column(default=1)
    accepted_at: Mapped[float] = mapped_column(Float)
    start_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    deadline_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    mode: Mapped[str] = mapped_column(String(20), default='online')
    sla: Mapped[str] = mapped_column(String(20), default='none')
    attempted_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    reached_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    cancelled_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    policy: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (ForeignKeyConstraint(['tenant_id','conversation_id'],['conversations.tenant_id','conversations.id']),
                      ForeignKeyConstraint(['tenant_id','owner_id'],['members.tenant_id','members.id']),
                      UniqueConstraint('tenant_id','conversation_id'))

class ContactAttempt(Scoped, Base):
    __tablename__ = 'contact_attempts'
    task_id: Mapped[str] = mapped_column(String(40))
    message_id: Mapped[str] = mapped_column(String(40))
    actor_id: Mapped[str] = mapped_column(String(40))
    evidence: Mapped[str] = mapped_column(String(40), default='web_durable_accepted')
    __table_args__ = (ForeignKeyConstraint(['tenant_id','task_id'],['tasks.tenant_id','tasks.id']),
                      ForeignKeyConstraint(['tenant_id','actor_id'],['members.tenant_id','members.id']),
                      ForeignKeyConstraint(['tenant_id','message_id'],['messages.tenant_id','messages.id']))

class Knowledge(Scoped, Base):
    __tablename__ = 'knowledge'
    family: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(250))
    content: Mapped[str] = mapped_column(Text)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    category: Mapped[str] = mapped_column(String(120), default='企业知识')
    status: Mapped[str] = mapped_column(String(25), default='draft')
    visibility: Mapped[str] = mapped_column(String(20), default='public')
    version: Mapped[int] = mapped_column(default=1)
    risk: Mapped[str] = mapped_column(String(20), default='low')
    source_url: Mapped[str] = mapped_column(String(1000), default='')
    author_id: Mapped[str] = mapped_column(String(40))
    reviewer_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    approvals: Mapped[list] = mapped_column(JSON, default=list)
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    digest: Mapped[str] = mapped_column(String(64), default='')
    quick_reply: Mapped[dict] = mapped_column(JSON, default=dict)
    case_recommendation: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint('tenant_id','family','version'),)

class KnowledgeSource(Scoped, Base):
    __tablename__ = 'knowledge_sources'
    url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str] = mapped_column(String(250))
    digest: Mapped[str] = mapped_column(String(64), default='')
    interval_hours: Mapped[int] = mapped_column(default=24)
    next_at: Mapped[float] = mapped_column(Float, default=stamp)
    active: Mapped[bool] = mapped_column(default=True)
    last_status: Mapped[str] = mapped_column(String(30), default='pending')
    __table_args__ = (UniqueConstraint('tenant_id','url'),)

class KnowledgeGap(Scoped, Base):
    __tablename__ = 'knowledge_gaps'
    category: Mapped[str] = mapped_column(String(120))
    count: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(20), default='open')
    knowledge_id: Mapped[str | None] = mapped_column(String(40), nullable=True)

class KBCollection(Scoped, Base):
    __tablename__ = 'kb_collections'
    name: Mapped[str] = mapped_column(String(120))
    __table_args__ = (UniqueConstraint('tenant_id','name'),)

class KBDocument(Scoped, Base):
    __tablename__ = 'kb_documents'
    collection_id: Mapped[str] = mapped_column(String(40))
    filename: Mapped[str] = mapped_column(String(250))
    title: Mapped[str] = mapped_column(String(250))
    visibility: Mapped[str] = mapped_column(String(20),default='internal')
    channel_ids: Mapped[list] = mapped_column(JSON,default=list)
    active_version: Mapped[str | None] = mapped_column(String(40),nullable=True)
    author_id: Mapped[str] = mapped_column(String(40))
    deleted: Mapped[bool] = mapped_column(default=False)
    __table_args__ = (ForeignKeyConstraint(['tenant_id','collection_id'],['kb_collections.tenant_id','kb_collections.id']),)

class KBDocumentVersion(Scoped, Base):
    __tablename__ = 'kb_document_versions'
    document_id: Mapped[str] = mapped_column(String(40))
    number: Mapped[int] = mapped_column(default=1)
    digest: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(30),default='queued')
    normalized: Mapped[dict] = mapped_column(JSON,default=dict)
    issues: Mapped[list] = mapped_column(JSON,default=list)
    index_fingerprint: Mapped[str] = mapped_column(String(64),default='')
    build_manifest: Mapped[dict] = mapped_column(JSON,default=dict)
    reviewer_id: Mapped[str | None] = mapped_column(String(40),nullable=True)
    approvals: Mapped[list] = mapped_column(JSON,default=list)
    __table_args__ = (ForeignKeyConstraint(['tenant_id','document_id'],['kb_documents.tenant_id','kb_documents.id']),UniqueConstraint('tenant_id','document_id','number'))

class KBChunk(Scoped, Base):
    __tablename__ = 'kb_chunks'
    document_id: Mapped[str] = mapped_column(String(40))
    version_id: Mapped[str] = mapped_column(String(40))
    position: Mapped[int] = mapped_column(default=0)
    title: Mapped[str] = mapped_column(String(250))
    content: Mapped[str] = mapped_column(Text)
    location: Mapped[dict] = mapped_column(JSON,default=dict)
    digest: Mapped[str] = mapped_column(String(64))
    vector: Mapped[list] = mapped_column(JSON,default=list)
    vector_digest: Mapped[str] = mapped_column(String(64),default='')
    __table_args__ = (ForeignKeyConstraint(['tenant_id','version_id'],['kb_document_versions.tenant_id','kb_document_versions.id']),ForeignKeyConstraint(['tenant_id','document_id'],['kb_documents.tenant_id','kb_documents.id']))

class KBJob(Scoped, Base):
    __tablename__ = 'kb_jobs'
    version_id: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(25),default='pending')
    stage: Mapped[str] = mapped_column(String(30),default='queued')
    progress: Mapped[int] = mapped_column(default=0)
    error: Mapped[str] = mapped_column(String(60),default='')
    attempts: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[float] = mapped_column(Float,default=0)
    lease_token: Mapped[str] = mapped_column(String(40),default='')
    __table_args__ = (ForeignKeyConstraint(['tenant_id','version_id'],['kb_document_versions.tenant_id','kb_document_versions.id']),)

class KBEngineConfig(Scoped, Base):
    __tablename__ = 'kb_engine_configs'
    config: Mapped[dict] = mapped_column(JSON,default=dict)
    cipher: Mapped[str] = mapped_column(Text,default='')

class Offer(Scoped, Base):
    __tablename__ = 'offers'
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    active: Mapped[bool] = mapped_column(default=True)
    knowledge_id: Mapped[str | None] = mapped_column(String(40), nullable=True)

class ModelConfig(Scoped, Base):
    __tablename__ = 'model_configs'
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    cipher: Mapped[str] = mapped_column(Text, default='')

class AIJob(Scoped, Base):
    __tablename__ = 'ai_jobs'
    conversation_id: Mapped[str] = mapped_column(String(40))
    message_id: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default='pending')
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    lease_token: Mapped[str] = mapped_column(String(40),default='')
    attempts: Mapped[int] = mapped_column(default=0)
    __table_args__ = (ForeignKeyConstraint(['tenant_id','conversation_id'],['conversations.tenant_id','conversations.id']),
                      ForeignKeyConstraint(['tenant_id','message_id'],['messages.tenant_id','messages.id']),
                      UniqueConstraint('tenant_id','message_id'))

class Usage(Scoped, Base):
    __tablename__ = 'usage_events'
    kind: Mapped[str] = mapped_column(String(30))
    tokens: Mapped[int] = mapped_column(default=0)
    outcome: Mapped[str] = mapped_column(String(40), default='')
    latency_ms: Mapped[int] = mapped_column(default=0)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)

class Outbox(Scoped, Base):
    __tablename__ = 'outbox_events'
    key: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(30))
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    version: Mapped[int] = mapped_column(default=1)
    due_at: Mapped[float] = mapped_column(Float, default=stamp)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    attempts: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(20), default='pending')
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    last_error: Mapped[str] = mapped_column(String(40), default='')
    __table_args__ = (UniqueConstraint('tenant_id','key'),)

class Notice(Scoped, Base):
    __tablename__ = 'notices'
    text: Mapped[str] = mapped_column(String(500))
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    kind: Mapped[str] = mapped_column(String(30), default='info')
    outbox_id: Mapped[str] = mapped_column(String(40), unique=True)

class Audit(Scoped, Base):
    __tablename__ = 'audit_events'
    actor: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(80))
    target: Mapped[str] = mapped_column(String(100), default='')
    detail: Mapped[dict] = mapped_column(JSON, default=dict)

class WorkerState(Base):
    __tablename__ = 'worker_state'
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    heartbeat_at: Mapped[float] = mapped_column(Float)

class Channel(Scoped, Base):
    __tablename__ = 'channels'
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(30))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    cipher: Mapped[str] = mapped_column(Text, default='')
    identity_key: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    version: Mapped[int] = mapped_column(default=1)
    enabled: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(30), default='draft')
    diagnostic: Mapped[str] = mapped_column(String(40), default='')
    tested_version: Mapped[int] = mapped_column(default=0)
    tested_at: Mapped[float] = mapped_column(Float, default=0)
    heartbeat_at: Mapped[float] = mapped_column(Float, default=0)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    lease_owner: Mapped[str] = mapped_column(String(40), default='')
    received_at: Mapped[float] = mapped_column(Float, default=0)
    sent_at: Mapped[float] = mapped_column(Float, default=0)
    runtime_cipher: Mapped[str] = mapped_column(Text, default='')
    poll_due: Mapped[float] = mapped_column(Float, default=0)

class ExternalThread(Scoped, Base):
    __tablename__ = 'external_threads'
    channel_id: Mapped[str] = mapped_column(String(40))
    conversation_id: Mapped[str] = mapped_column(String(40))
    external_key: Mapped[str] = mapped_column(String(64))
    route_cipher: Mapped[str] = mapped_column(Text)
    eligible_until: Mapped[float] = mapped_column(Float, default=0)
    __table_args__ = (ForeignKeyConstraint(['tenant_id','channel_id'],['channels.tenant_id','channels.id']),
                      ForeignKeyConstraint(['tenant_id','conversation_id'],['conversations.tenant_id','conversations.id']),
                      UniqueConstraint('tenant_id','channel_id','external_key'),UniqueConstraint('tenant_id','conversation_id'))

class ChannelReceipt(Scoped, Base):
    __tablename__ = 'channel_receipts'
    channel_id: Mapped[str] = mapped_column(String(40))
    event_key: Mapped[str] = mapped_column(String(64))
    conversation_id: Mapped[str] = mapped_column(String(40))
    __table_args__ = (ForeignKeyConstraint(['tenant_id','channel_id'],['channels.tenant_id','channels.id']),
                      ForeignKeyConstraint(['tenant_id','conversation_id'],['conversations.tenant_id','conversations.id']),
                      UniqueConstraint('tenant_id','channel_id','event_key'))

class ChannelDelivery(Scoped, Base):
    __tablename__ = 'channel_deliveries'
    channel_id: Mapped[str] = mapped_column(String(40))
    conversation_id: Mapped[str] = mapped_column(String(40))
    message_id: Mapped[str] = mapped_column(String(40))
    channel_version: Mapped[int] = mapped_column(default=1)
    task_version: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(25), default='pending')
    attempts: Mapped[int] = mapped_column(default=0)
    due_at: Mapped[float] = mapped_column(Float, default=stamp)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    last_error: Mapped[str] = mapped_column(String(40), default='')
    accepted_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    __table_args__ = (ForeignKeyConstraint(['tenant_id','channel_id'],['channels.tenant_id','channels.id']),
                      ForeignKeyConstraint(['tenant_id','conversation_id'],['conversations.tenant_id','conversations.id']),
                      ForeignKeyConstraint(['tenant_id','message_id'],['messages.tenant_id','messages.id']),
                      UniqueConstraint('tenant_id','message_id'))

class AnalyticsRecord(Scoped, Base):
    __tablename__ = 'analytics_records'
    data_kind: Mapped[str] = mapped_column(String(20), default='unknown')
    resolution: Mapped[str] = mapped_column(String(20), default='unknown')
    reviewed_sequence: Mapped[int] = mapped_column(Integer, default=0)
    evidence_message_id: Mapped[str] = mapped_column(String(40), default='')
    confirmed_all: Mapped[bool] = mapped_column(Boolean, default=False)
    reviews: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (ForeignKeyConstraint(['tenant_id','id'],['conversations.tenant_id','conversations.id']),)

class AnswerFeedback(Scoped, Base):
    __tablename__ = 'answer_feedback'
    conversation_id: Mapped[str] = mapped_column(String(40), index=True)
    value: Mapped[str] = mapped_column(String(20))
    __table_args__ = (ForeignKeyConstraint(['tenant_id','id'],['messages.tenant_id','messages.id']),
                      ForeignKeyConstraint(['tenant_id','conversation_id'],['conversations.tenant_id','conversations.id']),)

SCOPED_TABLES = [t.name for t in Base.metadata.sorted_tables if 'tenant_id' in t.c and t.name != 'auth_sessions']
