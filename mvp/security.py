import hashlib
import hmac
import secrets
import time
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import HTTPException, Request
from sqlalchemy import select
from .db import transaction, rows
from .models import AuthSession, Member, Tenant
from .config import settings

passwords = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
DUMMY = passwords.hash(secrets.token_urlsafe(30))
COOKIE = 'sc_mvp_session'
VISITOR_COOKIE = 'sc_mvp_visitor'
def session_cookie(visitor=False):
    return (VISITOR_COOKIE if visitor else COOKIE)+settings().get('cookie_suffix','')

def digest(value): return hashlib.sha256(value.encode()).hexdigest()

def hash_password(value):
    if len(value) < 12 or len(value) > 128:
        raise HTTPException(422, '密码请使用12–128个字符。')
    return passwords.hash(value)

def verify_password(encoded, value):
    try: return passwords.verify(encoded or DUMMY, value)
    except (VerificationError, InvalidHashError): return False

def issue(session, tenant, identity, kind,channel_id=None):
    token = secrets.token_urlsafe(40)
    csrf = secrets.token_urlsafe(32)
    session.add(AuthSession(token_hash=digest(token), tenant_id=tenant, identity=identity, kind=kind,
                            csrf=csrf,channel_id=channel_id, expires_at=time.time() + (86400*7 if kind=='visitor' else 28800)))
    return token, csrf

def principal(request: Request, *, write=False):
    public=request.url.path.startswith('/api/public/')
    token = (request.headers.get('Authorization','').removeprefix('Bearer ') or request.cookies.get(session_cookie(True),'')) if public else request.cookies.get(session_cookie(), '')
    if not token: raise HTTPException(401, '请先登录。')
    with transaction() as session:
        auth = session.get(AuthSession, digest(token))
        if not auth or auth.revoked or auth.expires_at <= time.time():
            raise HTTPException(401, '登录已失效，请重新登录。')
        if public != (auth.kind=='visitor'): raise HTTPException(401,'会话授权类型不正确。')
        tenant = session.get(Tenant, auth.tenant_id)
        if not tenant or not tenant.active: raise HTTPException(401,'企业空间已停用。')
        data = dict(tenant=auth.tenant_id, id=auth.identity, kind=auth.kind, csrf=auth.csrf, slug=tenant.slug, company=tenant.name,channel_id=auth.channel_id)
    if write and not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),data['csrf']):
        raise HTTPException(403,'操作校验失效，请刷新页面。')
    if data['kind'] == 'employee':
        with transaction(data['tenant']) as session:
            member = session.scalar(rows(session,Member,data['tenant']).where(Member.id==data['id']))
            if not member or not member.active: raise HTTPException(401,'账号已停用。')
            data.update(roles=member.roles, name=member.name)
    else: data.update(roles=['visitor'],name='访客')
    return data

def require(p, *roles):
    if not set(p['roles']).intersection(roles): raise HTTPException(403,'当前账号没有此操作权限。')

def serialize(row, exclude=()):
    return {column.name:getattr(row,column.name) for column in row.__table__.columns
            if not column.name.endswith('_cipher') and column.name not in {'tenant_id','password_hash','cipher','token_hash','identity_key','lease_owner'} | set(exclude)}
