import asyncio
import csv
import hmac
import io
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, func, text, case
from sqlalchemy.exc import SQLAlchemyError
from server import model_gateway
from .config import settings, ROOT
from .db import transaction, rows, locked, database_time, serialization_lock
from .models import *
from .security import principal, require, serialize, issue, digest, verify_password, hash_password, session_cookie
from .service import *
from .bootstrap import seed_company
from .reception import STAGES,stage_expression,task_presentation,notification_current
from .channels import router as channels_router,channel_for_session
from .channel_callbacks import router as callback_router
from .platform import router as platform_router
from .knowledge_routes import router as knowledge_router
from .realtime import message_bus
from .quick_routes import router as quick_router
from .analytics_routes import router as analytics_router
from .case_routes import router as case_router

@asynccontextmanager
async def lifespan(app):
    cfg=settings()
    with transaction() as session:
        flags=session.execute(text('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user')).one()
        if any(flags): raise RuntimeError('MVP runtime database role must not bypass RLS')
    message_bus.listener.start()
    try:yield
    finally:message_bus.listener.close()

app=FastAPI(title='SalesCare MVP',version='1.0.0',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
app.include_router(channels_router)
app.include_router(callback_router)
app.include_router(platform_router)
app.include_router(knowledge_router)
app.include_router(quick_router)
app.include_router(analytics_router)
app.include_router(case_router)

class Input(BaseModel): model_config=ConfigDict(extra='forbid')

def auth(request:Request): return principal(request,write=request.method not in ('GET','HEAD','OPTIONS'))

@app.exception_handler(RequestValidationError)
async def invalid(request,exc): return JSONResponse(status_code=422,content={'detail':'输入格式不正确，请检查字段长度与必填项。'})

@app.exception_handler(SQLAlchemyError)
async def database_failure(request,exc):
    return JSONResponse(status_code=503,content={'detail':'服务暂时无法保存数据，请保留内容后重试。'})

@app.middleware('http')
async def security_headers(request,call_next):
    try:content_length=int(request.headers.get('Content-Length','0') or 0)
    except ValueError:return JSONResponse(status_code=400,content={'detail':'请求格式不正确。'})
    upload_request=request.url.path=='/api/kb/documents/upload' and request.method=='POST'
    if upload_request and not request.headers.get('Content-Length'):
        return JSONResponse(status_code=411,content={'detail':'文件上传需要明确内容长度。'})
    if content_length>(20_250_000 if upload_request else 250000):
        return JSONResponse(status_code=413,content={'detail':'请求内容超过大小限制。'})
    if request.url.path.startswith('/api/demo'):
        return JSONResponse(status_code=404,content={'detail':'演示接口在 MVP 中不可用。'})
    origin=request.headers.get('Origin')
    if request.method not in ('GET','HEAD','OPTIONS') and origin:
        allowed={settings().get('public_origin')}
        if settings()['mode']!='production':allowed.update({'http://127.0.0.1:8767','http://localhost:8767'})
        if origin not in allowed: return JSONResponse(status_code=403,content={'detail':'请求来源未授权。'})
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='strict-origin-when-cross-origin'
    response.headers['Cache-Control']='no-store' if not request.url.path.startswith('/assets') else 'public,max-age=31536000,immutable'
    response.headers['Permissions-Policy']='camera=(),microphone=(),geolocation=()'
    frame="'none'"
    if request.url.path.startswith('/chat/'):
        slug=request.url.path.split('/')[2]
        with transaction() as session: tenant=session.scalar(select(Tenant).where(Tenant.slug==slug,Tenant.active==True))
        if tenant:
            with transaction(tenant.id) as session:
                frame=' '.join(["'self'",*company(session,tenant.id).allowed_origins])
                cid=request.query_params.get('channel')
                if cid:
                    ch=session.get(Channel,(tenant.id,cid))
                    frame=' '.join(["'self'",*(ch.config.get('allowed_origins',[]) if ch and ch.kind=='web' and ch.enabled else [])])
    response.headers['Content-Security-Policy']=f"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors {frame}"
    if settings()['mode']=='production': response.headers['Strict-Transport-Security']='max-age=31536000'
    return response

def set_cookie(response,token,visitor=False):
    response.set_cookie(session_cookie(visitor),token,httponly=True,secure=settings()['secure_cookies'],
                        samesite='lax',max_age=604800 if visitor else 28800,path='/')

@app.get('/api/health')
def health():
    with transaction() as session:
        session.execute(text('SELECT 1'))
        heartbeat=session.scalar(select(func.max(WorkerState.heartbeat_at)).where(WorkerState.id.notin_(['channel-gateway','knowledge-ingestion'])))
        channel_heartbeat=session.get(WorkerState,'channel-gateway')
        knowledge_heartbeat=session.get(WorkerState,'knowledge-ingestion')
    return {'ok':True,'mode':settings()['mode'],'storage':'postgresql','worker_ready':bool(heartbeat and heartbeat>time.time()-15),
            'channel_worker_ready':bool(channel_heartbeat and channel_heartbeat.heartbeat_at>time.time()-15),
            'knowledge_worker_ready':bool(knowledge_heartbeat and knowledge_heartbeat.heartbeat_at>time.time()-15)}

@app.get('/api/setup/status')
def setup_status():
    with transaction() as session: tenants=list(session.scalars(select(Tenant.id)))
    for tid in tenants:
        with transaction(tid) as session:
            if session.scalar(rows(session,Member,tid).where(Member.username==settings()['admin_username'])):
                return {'required':False}
    return {'required':True,'username':settings()['admin_username'],'name':settings()['admin_name']}

class Setup(Input):
    token:str=Field(max_length=100)
    password:str=Field(max_length=128)

@app.post('/api/setup')
def setup(body:Setup,request:Request):
    if request.client.host not in ('127.0.0.1','::1','testclient'):
        raise HTTPException(403,'请在服务电脑上使用本机初始化页面。')
    if not hmac.compare_digest(body.token,settings().get('setup_token','')):
        raise HTTPException(403,'初始化凭证不正确。')
    tid=seed_company()
    with transaction(tid) as session:
        serialization_lock(session,tid,'setup')
        if session.scalar(rows(session,Member,tid).limit(1)): raise HTTPException(409,'初始化已经完成。')
        member=Member(tenant_id=tid,username=settings()['admin_username'],name=settings()['admin_name'],
                      password_hash=hash_password(body.password),roles=['admin','editor','publisher'],skills=[])
        session.add(member);session.flush()
        audit(session,tid,member.id,'account.initialized',member.id)
    return {'ok':True,'message':'管理员已创建，请登录。'}

class LoginInput(Input):
    tenant:str=Field(default='biaofun',pattern=r'^[a-z0-9-]{1,80}$')
    username:str=Field(min_length=1,max_length=80)
    password:str=Field(min_length=1,max_length=128)

@app.post('/api/login')
def login(body:LoginInput,request:Request,response:Response):
    key=digest((request.client.host or '')+':'+body.tenant+':'+body.username.lower())
    with transaction() as session:
        serialization_lock(session,'auth',key)
        limit=session.get(LoginThrottle,key)
        if limit and limit.until>time.time() and limit.count>=8: raise HTTPException(429,'登录尝试过多，请稍后重试。')
        if not limit: limit=LoginThrottle(key=key,count=0,until=time.time()+900);session.add(limit)
        if limit.until<=time.time():limit.count=0;limit.until=time.time()+900
        limit.count+=1
        tenant=session.scalar(select(Tenant).where(Tenant.slug==body.tenant,Tenant.active==True))
    member=None
    if tenant:
        with transaction(tenant.id) as session:
            member=session.scalar(rows(session,Member,tenant.id).where(Member.username==body.username.lower(),Member.active==True))
    if not verify_password(member.password_hash if member else None,body.password): raise HTTPException(401,'企业、账号或密码不正确。')
    with transaction() as session:
        current=session.get(LoginThrottle,key);current.count=0
        token,csrf=issue(session,tenant.id,member.id,'employee')
    with transaction(tenant.id) as session: audit(session,tenant.id,member.id,'account.login',member.id)
    set_cookie(response,token)
    return {'csrf':csrf,'id':member.id,'name':member.name,'roles':member.roles,'company':tenant.name,'slug':tenant.slug}

@app.get('/api/me')
def me(p=Depends(auth)):
    require(p,'admin','agent','editor','publisher')
    with transaction(p['tenant']) as session:
        member=session.get(Member,(p['tenant'],p['id']))
        available=bool('agent' in member.roles and member.available and member.heartbeat_at>=database_time(session)-90)
    return {**{k:p[k] for k in ('name','roles','csrf','slug','company','id')},'available':available}

@app.post('/api/logout')
def logout(request:Request,response:Response,p=Depends(auth)):
    with transaction() as session:
        row=session.get(AuthSession,digest(request.cookies[session_cookie()]));row.revoked=True
    response.delete_cookie(session_cookie())
    return {'ok':True}

class PasswordInput(Input):
    current:str=Field(max_length=128)
    password:str=Field(max_length=128)

@app.post('/api/password')
def change_password(body:PasswordInput,p=Depends(auth)):
    require(p,'admin','agent','editor','publisher')
    with transaction(p['tenant']) as session:
        member=locked(session,Member,p['tenant'],p['id'])
        if not verify_password(member.password_hash,body.current): raise HTTPException(403,'原密码不正确。')
        member.password_hash=hash_password(body.password)
        audit(session,p['tenant'],p['id'],'account.password_changed',p['id'])
    with transaction() as session:
        for s in session.scalars(select(AuthSession).where(AuthSession.tenant_id==p['tenant'],AuthSession.identity==p['id'])): s.revoked=True
    return {'ok':True,'message':'密码已修改，请重新登录。'}

class InviteInput(Input):
    username:str=Field(pattern=r'^[a-z0-9._-]{3,80}$')
    name:str=Field(min_length=1,max_length=120)
    roles:list[str]=Field(min_length=1,max_length=4)

@app.get('/api/team')
def team(p=Depends(auth)):
    require(p,'admin','agent')
    with transaction(p['tenant']) as session:
        return [serialize(m,exclude=['username'] if 'admin' not in p['roles'] else []) for m in session.scalars(rows(session,Member,p['tenant']).order_by(Member.created_at))]

@app.post('/api/team/invitations')
def invite(body:InviteInput,p=Depends(auth)):
    require(p,'admin')
    if not set(body.roles)<= {'admin','agent','editor','publisher'}: raise HTTPException(422,'角色无效。')
    token=secrets.token_urlsafe(40)
    with transaction(p['tenant']) as session:
        serialization_lock(session,p['tenant'],'invite:'+body.username)
        if session.scalar(rows(session,Member,p['tenant']).where(Member.username==body.username)): raise HTTPException(409,'账号已存在。')
        session.add(Invitation(tenant_id=p['tenant'],username=body.username,name=body.name,roles=body.roles,
                               token_hash=digest(token),expires_at=time.time()+86400))
        audit(session,p['tenant'],p['id'],'account.invited',body.username)
    return {'url':settings()['public_origin']+'/invite/'+p['slug']+'#'+token,'expires_hours':24}

class AcceptInvite(Input):
    tenant:str=Field(max_length=80)
    token:str=Field(max_length=100)
    password:str=Field(max_length=128)

@app.post('/api/invitations/accept')
def accept_invite(body:AcceptInvite):
    with transaction() as session: tenant=session.scalar(select(Tenant).where(Tenant.slug==body.tenant,Tenant.active==True))
    if not tenant: raise HTTPException(404,'邀请无效。')
    with transaction(tenant.id) as session:
        row=session.scalar(rows(session,Invitation,tenant.id).where(Invitation.token_hash==digest(body.token)).with_for_update())
        if not row or row.used or row.expires_at<=time.time(): raise HTTPException(403,'邀请无效或已过期。')
        serialization_lock(session,tenant.id,'invite:'+row.username)
        if session.scalar(rows(session,Member,tenant.id).where(Member.username==row.username)): raise HTTPException(409,'账号已存在。')
        config=company(session,tenant.id)
        m=Member(tenant_id=tenant.id,username=row.username,name=row.name,password_hash=hash_password(body.password),roles=row.roles,
                  skills=[config.required_skill] if 'agent' in row.roles else [])
        session.add(m);row.used=True
        audit(session,tenant.id,'invite','account.created',row.username)
    return {'ok':True}

class MemberUpdate(Input):
    active:bool
    roles:list[str]=Field(min_length=1,max_length=4)
    skills:list[str]=Field(max_length=10)
    capacity:int=Field(ge=1,le=30)

@app.put('/api/team/{mid}')
def update_member(mid:str,body:MemberUpdate,p=Depends(auth)):
    require(p,'admin')
    if not set(body.roles)<= {'admin','agent','editor','publisher'}: raise HTTPException(422,'角色无效。')
    with transaction(p['tenant']) as session:
        serialization_lock(session,p['tenant'],'admins')
        m=locked(session,Member,p['tenant'],mid)
        if not m: raise HTTPException(404,'成员不存在。')
        if 'admin' in m.roles and m.active and (not body.active or 'admin' not in body.roles):
            admins=[a for a in session.scalars(rows(session,Member,p['tenant']).where(Member.active==True)) if 'admin' in a.roles]
            if len(admins)<=1: raise HTTPException(409,'不能停用最后一位管理员。')
        m.active=body.active;m.roles=body.roles;m.skills=body.skills;m.capacity=body.capacity
        if not m.active or 'agent' not in m.roles:m.available=False
        audit(session,p['tenant'],p['id'],'account.updated',mid)
    return {'ok':True}

class Presence(Input): available:bool

@app.post('/api/presence')
def presence(body:Presence,p=Depends(auth)):
    require(p,'agent')
    with transaction(p['tenant']) as session:
        m=locked(session,Member,p['tenant'],p['id']);m.available=body.available;m.heartbeat_at=database_time(session)
    return {'ok':True}

@app.get('/api/settings')
def get_settings(p=Depends(auth)):
    require(p,'admin')
    with transaction(p['tenant']) as session:
        cfg=company(session,p['tenant'])
        options=json.loads(model_gateway.protect(cfg.notification_options_cipher,decrypt=True)) if cfg.notification_options_cipher else {}
        return {**serialize(cfg),'notification_configured':bool(cfg.notification_cipher),'notification_keyword':options.get('keyword',''),
                'notification_signed':bool(options.get('signing_secret')),'preflight':check_ready(session,p['tenant'])}

class SettingsInput(Input):
    tagline:str=Field(max_length=300)
    service_mode:str|None=None
    weekdays:list[int]=Field(min_length=1,max_length=7)
    opens:str=Field(pattern=r'^\d{2}:\d{2}$')
    closes:str=Field(pattern=r'^\d{2}:\d{2}$')
    required_skill:str=Field(min_length=1,max_length=120)
    allowed_origins:list[str]=Field(max_length=20)
    source_hosts:list[str]=Field(max_length=20)
    published:bool
    daily_ai_limit:int=Field(ge=0,le=100000)
    daily_token_limit:int=Field(ge=0,le=100000000)
    ai_concurrency:int=Field(ge=1,le=10)
    retention_days:int=Field(ge=7,le=3650)

@app.put('/api/settings')
def update_settings(body:SettingsInput,p=Depends(auth)):
    require(p,'admin')
    if body.service_mode not in (None,'sales_consulting','general_support'):raise HTTPException(422,'服务模式无效。')
    try:
        datetime.strptime(body.opens,'%H:%M');datetime.strptime(body.closes,'%H:%M')
    except ValueError: raise HTTPException(422,'服务时间不正确。') from None
    if body.opens>=body.closes or any(x not in range(7) for x in body.weekdays):raise HTTPException(422,'服务时段不正确。')
    origins=[]
    for origin in body.allowed_origins:
        u=urlsplit(origin)
        if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.path not in ('','/') or u.query or u.fragment:
            raise HTTPException(422,'嵌入域名请填写完整网站源地址。')
        if settings()['mode']=='production' and u.scheme!='https': raise HTTPException(422,'生产嵌入域名必须使用HTTPS。')
        origins.append(origin.rstrip('/'))
    if any(not re.fullmatch(r'[a-zA-Z0-9.-]{3,200}',x) for x in body.source_hosts):raise HTTPException(422,'知识白名单域名无效。')
    with transaction(p['tenant']) as session:
        config=company(session,p['tenant'])
        for k,v in body.model_dump().items():
            if k=='service_mode' and v is None:continue
            setattr(config,k,v)
        config.allowed_origins=origins
        if config.published and not check_ready(session,p['tenant'])['knowledge']:raise HTTPException(409,'请先发布有效企业知识。')
        audit(session,p['tenant'],p['id'],'company.configured')
    return {'ok':True}

class NotificationInput(Input):
    kind:str
    webhook:str=Field(default='',max_length=1000)
    signing_secret:str=Field(default='',max_length=300)
    keyword:str=Field(default='',max_length=50)

@app.put('/api/notifications/config')
def notification_config(body:NotificationInput,p=Depends(auth)):
    require(p,'admin')
    if body.kind not in ('none','wecom','dingtalk'):raise HTTPException(422,'通知类型无效。')
    if body.webhook:
        u=urlsplit(body.webhook)
        expected='qyapi.weixin.qq.com' if body.kind=='wecom' else 'oapi.dingtalk.com'
        if u.scheme!='https' or u.hostname!=expected or u.username or u.password or u.port not in (None,443):raise HTTPException(422,'请填写官方机器人Webhook地址。')
    with transaction(p['tenant']) as session:
        config=company(session,p['tenant'])
        changed=body.kind!=config.notification_kind
        changed=changed or bool(body.webhook and config.notification_cipher and model_gateway.protect(config.notification_cipher,decrypt=True)!=body.webhook)
        config.notification_kind=body.kind
        if body.kind=='none':config.notification_cipher=''
        elif body.webhook:config.notification_cipher=model_gateway.protect(body.webhook)
        elif changed:config.notification_cipher=''
        options={} if changed or not config.notification_options_cipher else json.loads(model_gateway.protect(config.notification_options_cipher,decrypt=True))
        if body.signing_secret.strip():options['signing_secret']=body.signing_secret.strip()
        options['keyword']=body.keyword.strip()
        config.notification_options_cipher=model_gateway.protect(json.dumps(options)) if body.kind!='none' else ''
        audit(session,p['tenant'],p['id'],'notification.configured')
    return {'ok':True}

@app.post('/api/notifications/test')
def notification_test(p=Depends(auth)):
    require(p,'admin')
    with transaction(p['tenant']) as session:
        config=company(session,p['tenant'])
        if not config.notification_cipher:raise HTTPException(409,'请先配置真实通知渠道。')
        event=Outbox(tenant_id=p['tenant'],key='test:'+uid(),kind='test',payload={'text':'通知连接测试，请确认收到本条提醒。'})
        session.add(event);session.flush()
        return {'id':event.id,'status':'pending'}

@app.get('/api/notices')
def notices(p=Depends(auth)):
    require(p,'admin','agent')
    with transaction(p['tenant']) as session:
        items=[]
        for n in session.scalars(rows(session,Notice,p['tenant']).order_by(Notice.created_at.desc()).limit(30)):
            event=session.get(Outbox,(p['tenant'],n.outbox_id))
            task=session.get(Task,(p['tenant'],n.task_id)) if n.task_id else None
            current=bool(event and notification_current(task,event))
            items.append({**serialize(n),'current':current,'conversation_id':task.conversation_id if task else None,
                          'delivery':event.status if event else 'unknown'})
        return {'items':items,
                'failures':session.scalar(select(func.count()).select_from(Outbox).where(Outbox.tenant_id==p['tenant'],Outbox.status=='failed'))}

@app.get('/api/notification-events')
def notification_events(p=Depends(auth)):
    require(p,'admin')
    with transaction(p['tenant']) as session:
        events=session.scalars(rows(session,Outbox,p['tenant']).order_by(Outbox.created_at.desc()).limit(50)).all()
        result=[]
        for event in events:
            task=session.get(Task,(p['tenant'],event.task_id)) if event.task_id else None
            result.append({'id':event.id,'kind':event.kind,'status':event.status,'attempts':event.attempts,'last_error':event.last_error,
                           'created_at':event.created_at,'due_at':event.due_at,'current':notification_current(task,event),
                           'conversation_id':task.conversation_id if task else None})
        counts=dict(session.execute(select(Outbox.status,func.count()).where(Outbox.tenant_id==p['tenant']).group_by(Outbox.status)).all())
        return {'items':result,'counts':counts,'configured':bool(company(session,p['tenant']).notification_cipher)}

@app.get('/api/reception-summary')
def reception_summary(p=Depends(auth)):
    require(p,'admin','agent');tid=p['tenant']
    with transaction(tid) as session:
        now=database_time(session)
        stage=case((Conversation.state=='CLOSED','closed'),else_=stage_expression(now))
        grouped=session.execute(select(stage,func.count()).select_from(Conversation)
                                .outerjoin(Task,(Task.tenant_id==Conversation.tenant_id)&(Task.conversation_id==Conversation.id))
                                .where(Conversation.tenant_id==tid).group_by(stage)).all()
        counts={key:0 for key in STAGES};counts.update(dict(grouped))
        attention=session.scalar(select(func.count()).select_from(Task).outerjoin(Member,(Member.tenant_id==Task.tenant_id)&(Member.id==Task.owner_id))
                                 .where(Task.tenant_id==tid,Task.status=='claimed',
                                        (Member.id.is_(None))|(~Member.active)|(~Member.available)|(Member.heartbeat_at<now-90)))
        return {'counts':counts,'owner_attention':attention,'server_now':now,'worker_ready':health()['worker_ready']}

@app.get('/api/conversations')
def conversations(page:int=1,q:str='',stage:str='all',p=Depends(auth)):
    require(p,'admin','agent')
    page=max(1,min(page,10000))
    with transaction(p['tenant']) as session:
        if len(q)>80:raise HTTPException(422,'搜索条件过长。')
        if stage not in {'all','mine',*STAGES}:raise HTTPException(422,'接待筛选条件无效。')
        now=database_time(session)
        queue=case((Conversation.state=='CLOSED','closed'),else_=stage_expression(now))
        statement=rows(session,Conversation,p['tenant']).outerjoin(Task,(Task.tenant_id==Conversation.tenant_id)&(Task.conversation_id==Conversation.id))
        if q:statement=statement.where(Conversation.name.ilike('%'+q+'%'))
        if stage=='mine':statement=statement.where(Task.owner_id==p['id'],Task.status.in_(['pending','claimed']))
        elif stage!='all':statement=statement.where(queue==stage)
        priority=case((queue=='overdue',0),(queue=='first_reply',1),(queue=='waiting',2),(queue=='followup',3),(queue=='appointment',4),(queue=='ai',5),else_=6)
        statement=statement.order_by(priority,Task.deadline_at.asc().nullslast(),Conversation.created_at.desc())
        results=session.scalars(statement.offset((page-1)*30).limit(30)).all()
        out=[]
        for c in results:
            task=get_task(session,p['tenant'],c.id)
            owner=session.get(Member,(p['tenant'],task.owner_id)) if task and task.owner_id else None
            view={**serialize(task),**task_presentation(task,owner,now)} if task else None
            out.append({'id':c.id,'name':c.name,'state':c.state,'created_at':c.created_at,'sequence':c.sequence,'task':view})
        return out

@app.get('/api/conversations/{cid}')
def conversation_detail(cid:str,p=Depends(auth)):
    with transaction(p['tenant']) as session:return conversation_view(session,p,get_conversation(session,p,cid))

class TaskAction(Input):
    version:int=Field(ge=1)
    owner_id:str|None=Field(default=None,max_length=40)

@app.post('/api/tasks/{task_id}/actions/{action}')
def task_action(task_id:str,action:str,body:TaskAction,p=Depends(auth)):
    require(p,'agent','admin')
    if action not in ('claim','transfer','close'):raise HTTPException(404,'操作不存在。')
    tid=p['tenant']
    with transaction(tid) as session:
        serialization_lock(session,tid,'agent-capacity')
        reference=session.get(Task,(tid,task_id))
        if not reference:raise HTTPException(404,'任务不存在。')
        c=locked(session,Conversation,tid,reference.conversation_id)
        task=locked(session,Task,tid,task_id)
        if task.version!=body.version:raise HTTPException(409,'任务归属已变化，请刷新后重试。')
        if task.status in ('cancelled','closed'):raise HTTPException(409,'任务已结束。')
        now=database_time(session)
        choices=candidates(session,tid,task.skill,now)
        if action=='claim':
            require(p,'agent')
            if task.status=='claimed':raise HTTPException(409,'此任务已被接待。')
            if not any(m.id==p['id'] for m in choices):
                member=session.get(Member,(tid,p['id']))
                if not member or not member.active or 'agent' not in member.roles:
                    raise HTTPException(403,'专业顾问职责已失效，请刷新或重新登录。')
                if task.skill not in member.skills:
                    raise HTTPException(409,'技能不匹配：本次接待需要「'+task.skill+'」。请在团队与服务的成员配置中加入此技能。')
                if not member.available:
                    raise HTTPException(409,'您当前处于接待离线状态，请先勾选右上角「接待在线」。')
                if member.heartbeat_at<now-90:
                    raise HTTPException(409,'在线连接已过期，请重新切换「接待在线」后重试。')
                load=session.scalar(select(func.count()).select_from(Task).where(Task.tenant_id==tid,Task.owner_id==member.id,Task.status=='claimed'))
                if load>=member.capacity:
                    raise HTTPException(409,f'接待容量已满（{load}/{member.capacity}），请先结束已完成的接待或由管理员调整容量。')
                raise HTTPException(409,'接待资格已变化，请刷新后重试。')
            task.owner_id=p['id'];task.status='claimed';c.state='HUMAN_ACTIVE'
        else:
            if task.owner_id!=p['id'] and 'admin' not in p['roles']:raise HTTPException(403,'只能处理自己接待的任务。')
            if action=='transfer':
                if not any(m.id==body.owner_id for m in choices):raise HTTPException(409,'目标顾问当前不可接待。')
                task.owner_id=body.owner_id;task.status='pending';c.state='HANDOFF_PENDING'
            else:task.status='closed';c.state='CLOSED'
        task.version+=1
        audit(session,tid,p['id'],'task.'+action,task.id)
        if action=='transfer':session.add(Outbox(tenant_id=tid,key=f'transfer:{task.id}:{task.version}',kind='transfer',task_id=task.id,version=task.version,payload={'text':'有转派接待需求，请进入工作台查看。'}))
        return serialize(task)

class TextInput(Input):
    text:str=Field(min_length=1,max_length=2000)
    client_id:str=Field(min_length=1,max_length=100)

class ContactInput(TextInput): version:int=Field(ge=1)

@app.post('/api/tasks/{task_id}/contact')
def contact(task_id:str,body:ContactInput,p=Depends(auth)):
    require(p,'agent');tid=p['tenant']
    if not body.text.strip():raise HTTPException(422,'请输入回复内容。')
    with transaction(tid) as session:
        reference=session.get(Task,(tid,task_id))
        if not reference:raise HTTPException(404,'任务不存在。')
        c=locked(session,Conversation,tid,reference.conversation_id)
        task=locked(session,Task,tid,task_id)
        previous=session.scalar(rows(session,Message,tid).where(Message.conversation_id==c.id,Message.client_id==body.client_id))
        if previous:
            if previous.text!=body.text.strip() or previous.actor_id!=p['id']:raise HTTPException(409,'重试编号与消息不一致。')
            delivery=session.scalar(rows(session,ChannelDelivery,tid).where(ChannelDelivery.message_id==previous.id))
            return {**serialize(previous),**({'delivery_status':delivery.status} if delivery else {})}
        if task.version!=body.version or task.owner_id!=p['id'] or task.status!='claimed':raise HTTPException(409,'请先接待当前任务，或刷新最新归属。')
        member=session.get(Member,(tid,p['id']))
        if not member.active or task.skill not in member.skills:raise HTTPException(403,'账号不具备当前任务专业资格。')
        lead=session.scalar(rows(session,Lead,tid).where(Lead.conversation_id==c.id))
        if c.state=='CLOSED' or task.cancelled_at or (lead and lead.withdrawn_at):raise HTTPException(409,'用户已停止联系。')
        now=database_time(session)
        if task.mode=='scheduled' and now<task.start_at:raise HTTPException(409,'尚未到用户确认的预约时间。')
        m=add_message(session,c,'human',body.text.strip(),actor_id=p['id'],actor_name=p['name'],client_id=body.client_id)
        external=bool(c.channel_id and c.channel!='web')
        if not external:session.add(ContactAttempt(tenant_id=tid,task_id=task.id,message_id=m.id,actor_id=p['id'],created_at=now))
        if not external and task.attempted_at is None:
            task.attempted_at=now
            if task.deadline_at:task.sla='met' if now<=task.deadline_at else 'breached'
        audit(session,tid,p['id'],'contact.queued' if external else 'contact.accepted',m.id,{'evidence':'platform_pending' if external else 'web_durable_accepted'})
        return {**serialize(m),**({'delivery_status':'pending'} if external else {})}

@app.get('/api/events')
async def events(request:Request,p=Depends(auth)):
    require(p,'admin','agent')
    async def stream():
        for _ in range(25):
            if await request.is_disconnected():break
            try:
                current=principal(request)
                require(current,'admin','agent')
            except HTTPException:break
            with transaction(current['tenant']) as session:
                latest=session.scalar(select(func.max(Message.created_at)).where(Message.tenant_id==current['tenant'])) or 0
                notices=session.scalar(select(func.max(Notice.created_at)).where(Notice.tenant_id==current['tenant'])) or 0
                version=session.scalar(select(func.max(Task.version)).where(Task.tenant_id==current['tenant'])) or 0
            yield 'event: update\ndata: '+json.dumps({'messages_at':latest,'notices_at':notices,'task_version':version})+'\n\n'
            await asyncio.sleep(2)
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})

def public_tenant(slug):
    with transaction() as session:tenant=session.scalar(select(Tenant).where(Tenant.slug==slug,Tenant.active==True))
    if not tenant:raise HTTPException(404,'咨询入口不存在。')
    with transaction(tenant.id) as session:
        if not company(session,tenant.id).published:raise HTTPException(409,'企业咨询入口尚未发布。')
    return tenant

@app.post('/api/public/session/{slug}')
def visitor_session(slug:str,request:Request,response:Response,channel:str|None=None):
    tenant=public_tenant(slug)
    if channel:
        with transaction(tenant.id) as session:channel_for_session(session,{'tenant':tenant.id,'channel_id':channel})
    key=digest('visitor:'+request.client.host+':'+slug)
    with transaction() as session:
        serialization_lock(session,'public-session',key)
        rate=session.get(LoginThrottle,key)
        if not rate:rate=LoginThrottle(key=key,count=0,until=time.time()+3600);session.add(rate)
        if rate.until<=time.time():rate.count=0;rate.until=time.time()+3600
        if rate.count>=100:raise HTTPException(429,'咨询入口访问较频繁，请稍后再试。')
        rate.count+=1
        token,csrf=issue(session,tenant.id,uid(),'visitor',channel)
    set_cookie(response,token,visitor=True)
    return {'csrf':csrf,'token':token,'company':tenant.name,'slug':tenant.slug,'channel_id':channel}

@app.get('/api/public/me')
def visitor_me(p=Depends(auth)):
    require(p,'visitor')
    return {'csrf':p['csrf'],'company':p['company'],'slug':p['slug'],'channel_id':p.get('channel_id')}

@app.get('/api/public/config')
def public_config(p=Depends(auth)):
    require(p,'visitor')
    with transaction(p['tenant']) as session:
        config=company(session,p['tenant'])
        channel=channel_for_session(session,p)
        now=database_time(session)
        return {'company':p['company'],'tagline':config.tagline,'service_mode':config.service_mode,'immediate':check_ready(session,p['tenant'])['immediate_ready'],
                'schedule':appointment_slots(config,now),'hours':'周一至周五 09:00–18:00' if config.weekdays==[0,1,2,3,4] and config.opens=='09:00' and config.closes=='18:00' else config.opens+'–'+config.closes,
                'consent':CONSENT,'welcome':channel.config.get('welcome','') if channel else '',
                'color':channel.config.get('color','#167461') if channel else '#167461',
                'sdk':{k:channel.config.get(k) for k in ('color','position','button_text','allowed_origins')} if channel else {}}

@app.get('/api/public/conversations')
def public_conversations(p=Depends(auth)):
    require(p,'visitor')
    with transaction(p['tenant']) as session:
        return [serialize(c) for c in session.scalars(rows(session,Conversation,p['tenant']).where(Conversation.visitor_id==p['id']).order_by(Conversation.created_at.desc()).limit(30))]

@app.post('/api/public/conversations')
def create_conversation(p=Depends(auth)):
    require(p,'visitor')
    with transaction(p['tenant']) as session:
        serialization_lock(session,p['tenant'],'visitor:'+p['id'])
        count=session.scalar(select(func.count()).select_from(Conversation).where(Conversation.tenant_id==p['tenant'],Conversation.visitor_id==p['id'],Conversation.created_at>time.time()-3600))
        if count>=10:raise HTTPException(429,'新建咨询较频繁，请稍后再试。')
        ch=channel_for_session(session,p)
        c=Conversation(tenant_id=p['tenant'],visitor_id=p['id'],channel_id=ch.id if ch else None);session.add(c);session.flush()
        add_message(session,c,'ai',(ch.config.get('welcome') if ch else '') or f"您好，这里是{p['company']}。请告诉我您的需求，我会根据企业已确认资料解答，也可以帮您联系专业顾问。")
        return serialize(c)

@app.get('/api/public/conversations/{cid}')
def public_detail(cid:str,p=Depends(auth)):
    require(p,'visitor')
    with transaction(p['tenant']) as session:return conversation_view(session,p,get_conversation(session,p,cid))

@app.post('/api/public/conversations/{cid}/messages')
def visitor_message(cid:str,body:TextInput,p=Depends(auth)):
    require(p,'visitor');tid=p['tenant']
    if not body.text.strip():raise HTTPException(422,'请输入咨询内容。')
    with transaction(tid) as session:
        c=get_conversation(session,p,cid,for_update=True)
        previous=session.scalar(rows(session,Message,tid).where(Message.conversation_id==cid,Message.client_id==body.client_id))
        if previous:
            if previous.text!=body.text.strip() or previous.role!='visitor':raise HTTPException(409,'重试编号与消息不一致。')
            return {'message':serialize(previous),'duplicate':True}
        if c.state=='CLOSED':raise HTTPException(409,'此会话已结束，请新建咨询。')
        channel_for_session(session,p)
        recent=session.scalar(select(func.count()).select_from(Message).where(Message.tenant_id==tid,Message.conversation_id==cid,Message.role=='visitor',Message.created_at>time.time()-60))
        if recent>=20:raise HTTPException(429,'发送较频繁，请稍后重试。')
        if session.scalar(rows(session,AIJob,tid).where(AIJob.conversation_id==cid,AIJob.status.in_(['pending','leased']))):raise HTTPException(409,'上一条咨询正在处理，请稍后发送。')
        m=add_message(session,c,'visitor',body.text.strip(),client_id=body.client_id)
        mark_profile(session,c,body.text)
        if c.state in ('AI_ACTIVE','HANDOFF_PENDING'):session.add(AIJob(tenant_id=tid,conversation_id=cid,message_id=m.id))
        else:
            task=get_task(session,tid,cid)
            if task and task.attempted_at and task.reached_at is None:task.reached_at=database_time(session)
        return {'message':serialize(m),'duplicate':False}

@app.post('/api/public/conversations/{cid}/handoff')
def visitor_handoff(cid:str,p=Depends(auth)):
    require(p,'visitor')
    with transaction(p['tenant']) as session:
        c=get_conversation(session,p,cid,for_update=True)
        if c.state=='CLOSED':raise HTTPException(409,'会话已结束，请新建咨询。')
        task=handoff(session,p,c)
        return {'id':task.id,'status':task.status}

class LeadInput(Input):
    name:str=Field(min_length=1,max_length=80)
    phone:str=Field(default='',max_length=30)
    consent:bool
    schedule_at:float|None=None

@app.post('/api/public/conversations/{cid}/lead')
def visitor_lead(cid:str,body:LeadInput,p=Depends(auth)):
    require(p,'visitor')
    if not body.name.strip() or (body.phone and not re.fullmatch(r'[+0-9 ()-]{6,25}',body.phone)):raise HTTPException(422,'称呼或备用手机号格式不正确。')
    with transaction(p['tenant']) as session:
        c=get_conversation(session,p,cid,for_update=True)
        if c.state=='CLOSED':raise HTTPException(409,'会话已结束，请新建咨询。')
        task=handoff(session,p,c,body.model_dump())
        return {'id':task.id,'mode':task.mode,'status':'submitted'}

@app.post('/api/public/conversations/{cid}/withdraw')
def visitor_withdraw(cid:str,p=Depends(auth)):
    require(p,'visitor')
    with transaction(p['tenant']) as session:withdraw(session,p,get_conversation(session,p,cid,for_update=True))
    return {'ok':True}

@app.post('/api/public/conversations/{cid}/delete')
def visitor_delete(cid:str,p=Depends(auth)):
    require(p,'visitor')
    with transaction(p['tenant']) as session:
        c=get_conversation(session,p,cid,for_update=True);withdraw(session,p,c)
        c.name='已删除访客';c.profile={}
        for m in session.scalars(rows(session,Message,p['tenant']).where(Message.conversation_id==cid)):
            m.text='[用户已删除内容]';m.citations=[];m.case_recommendations=[];m.actor_name='' if m.role=='visitor' else m.actor_name
        lead=session.scalar(rows(session,Lead,p['tenant']).where(Lead.conversation_id==cid))
        if lead:lead.name='已删除访客'
        for feedback in session.scalars(rows(session,AnswerFeedback,p['tenant']).where(AnswerFeedback.conversation_id==cid)):session.delete(feedback)
        annotation=session.get(AnalyticsRecord,(p['tenant'],cid))
        if annotation:
            annotation.resolution='unknown';annotation.evidence_message_id='';annotation.confirmed_all=False;annotation.reviews={}
        audit(session,p['tenant'],p['id'],'privacy.deleted',cid)
    return {'ok':True}

@app.get('/api/public/conversations/{cid}/events')
async def public_events(cid:str,request:Request,p=Depends(auth)):
    require(p,'visitor')
    try:cursor=max(0,int(request.headers.get('Last-Event-ID',request.query_params.get('cursor','0'))))
    except ValueError:raise HTTPException(422,'消息游标无效。')
    with transaction(p['tenant']) as session:get_conversation(session,p,cid)
    async def stream():
        position=cursor
        key,wake=message_bus.subscribe(p['tenant'],cid)
        try:
            while True:
                wake.clear()  # Clear before DB read: notifications during the read are retained.
                if await request.is_disconnected():break
                try:
                    current=principal(request)
                    with transaction(current['tenant']) as session:
                        c=get_conversation(session,current,cid)
                        messages=session.scalars(rows(session,Message,current['tenant']).where(Message.conversation_id==cid,Message.sequence>position).order_by(Message.sequence).limit(100)).all()
                        from .case_recommendations import public_cards
                        payload=[{**serialize(m),'case_recommendations':public_cards(session,current['tenant'],c.channel_id,m.case_recommendations)} for m in messages]
                        if payload:position=payload[-1]['sequence']
                        case_messages=session.scalars(rows(session,Message,current['tenant']).where(
                            Message.conversation_id==cid,Message.sequence<=position,
                            func.json_array_length(Message.case_recommendations)>0).order_by(Message.sequence.desc()).limit(100)).all()
                        case_updates=[{'message_id':m.id,'case_recommendations':public_cards(session,current['tenant'],c.channel_id,m.case_recommendations)} for m in case_messages]
                        state=c.state
                except HTTPException:break
                yield f'id: {position}\nevent: messages\ndata: '+json.dumps({'items':payload,'state':state,'case_updates':case_updates},ensure_ascii=False)+'\n\n'
                if len(payload)>=100:continue  # Drain backlog without waiting for another notification.
                try:await asyncio.wait_for(wake.wait(),timeout=2)
                except asyncio.TimeoutError:pass  # Auth refresh and missed-notification fallback.
        finally:message_bus.unsubscribe(key,wake)
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})

@app.post('/api/public/conversations/{cid}/received/{sequence}')
def received(cid:str,sequence:int,p=Depends(auth)):
    require(p,'visitor')
    with transaction(p['tenant']) as session:
        get_conversation(session,p,cid)
        for m in session.scalars(rows(session,Message,p['tenant']).where(Message.conversation_id==cid,Message.sequence<=sequence,Message.role=='human',Message.received_at.is_(None))):m.received_at=database_time(session)
    return {'ok':True}

@app.get('/api/knowledge')
def knowledge_list(p=Depends(auth)):
    require(p,'admin','editor','publisher')
    with transaction(p['tenant']) as session:
        return [serialize(k) for k in session.scalars(rows(session,Knowledge,p['tenant']).order_by(Knowledge.created_at.desc()))]

class KnowledgeInput(Input):
    title:str=Field(min_length=1,max_length=250)
    content:str=Field(min_length=10,max_length=20000)
    keywords:list[str]=Field(default=[],max_length=30)
    category:str=Field(default='企业知识',max_length=120)
    source_url:str=Field(default='',max_length=1000)
    visibility:str='public'
    expires_at:float|None=None
    family:str|None=Field(default=None,max_length=40)

@app.post('/api/knowledge')
def knowledge_create(body:KnowledgeInput,p=Depends(auth)):
    require(p,'admin','editor')
    if body.visibility not in ('public','internal'):raise HTTPException(422,'知识可见范围无效。')
    if body.source_url and (urlsplit(body.source_url).scheme!='https' or urlsplit(body.source_url).username or urlsplit(body.source_url).password):raise HTTPException(422,'知识来源请填写HTTPS页面。')
    tid=p['tenant']
    with transaction(tid) as session:
        family=body.family or uid();serialization_lock(session,tid,'knowledge:'+family)
        existing=session.scalar(rows(session,Knowledge,tid).where(Knowledge.family==family).order_by(Knowledge.version.desc()))
        if body.family and not existing:raise HTTPException(404,'原知识不存在。')
        if existing and session.get(KBChunk,(tid,existing.id)):raise HTTPException(409,'文档分块请在资料上传与构建中上传新版本。')
        values=body.model_dump(exclude={'family'})
        item=Knowledge(tenant_id=tid,family=family,version=(existing.version+1 if existing else 1),
                       author_id=p['id'],risk=risk(body.content+body.title),**values)
        session.add(item);session.flush();audit(session,tid,p['id'],'knowledge.created',item.id)
        return serialize(item)

class KnowledgeAction(Input): version:int=Field(ge=1)

@app.post('/api/knowledge/{kid}/{action}')
def knowledge_action(kid:str,action:str,body:KnowledgeAction,p=Depends(auth)):
    require(p,'admin','publisher')
    if action not in ('publish','withdraw'):raise HTTPException(404,'操作不存在。')
    tid=p['tenant']
    with transaction(tid) as session:
        reference=session.get(Knowledge,(tid,kid))
        if not reference:raise HTTPException(404,'知识不存在。')
        if session.get(KBChunk,(tid,kid)):raise HTTPException(409,'文档分块请在资料上传与构建中发布或撤回整份资料。')
        serialization_lock(session,tid,'knowledge:'+reference.family)
        item=locked(session,Knowledge,tid,kid)
        if item.version!=body.version:raise HTTPException(409,'知识版本已变化。')
        if action=='publish':
            if item.status not in ('draft','superseded','withdrawn'):raise HTTPException(409,'此版本已发布。')
            if item.risk=='high' and item.author_id==p['id']:raise HTTPException(403,'高风险知识必须由另一位发布者审批。')
            if item.risk=='high' and not session.get(Member,(tid,item.author_id)):
                approvals=list(item.approvals or [])
                if len({a['actor'] for a in approvals})<2:
                    if any(a['actor']==p['id'] for a in approvals):raise HTTPException(409,'您已审核此候选，需另一位发布者完成复核。')
                    approvals.append({'actor':p['id'],'at':database_time(session)})
                    item.approvals=approvals
                if len({a['actor'] for a in approvals})<2:
                    audit(session,tid,p['id'],'knowledge.review_recorded',kid,{'version':item.version})
                    return serialize(item)
            else:item.approvals=[{'actor':p['id'],'at':database_time(session)}]
            for old in session.scalars(rows(session,Knowledge,tid).where(Knowledge.family==item.family,Knowledge.status=='published').with_for_update()):old.status='superseded'
            session.flush();item.status='published';item.reviewer_id=p['id']
        else:item.status='withdrawn'
        audit(session,tid,p['id'],'knowledge.'+action,kid,{'version':item.version})
        return serialize(item)

class ImportText(Input):
    filename:str=Field(max_length=200)
    content:str=Field(min_length=10,max_length=20000)

@app.post('/api/knowledge-import')
def import_text(body:ImportText,p=Depends(auth)):
    if not body.filename.lower().endswith(('.txt','.md','.markdown')):raise HTTPException(422,'首版支持TXT和Markdown资料。')
    return knowledge_create(KnowledgeInput(title=body.filename,content=body.content,category='导入资料'),p)

@app.get('/api/knowledge-sources')
def source_list(p=Depends(auth)):
    require(p,'admin','editor','publisher')
    with transaction(p['tenant']) as session:return [serialize(s) for s in session.scalars(rows(session,KnowledgeSource,p['tenant']))]

class SourceInput(Input):
    url:str=Field(max_length=1000)
    title:str=Field(min_length=1,max_length=250)
    interval_hours:int=Field(default=24,ge=1,le=168)

@app.post('/api/knowledge-sources')
def create_source(body:SourceInput,p=Depends(auth)):
    require(p,'admin','editor')
    with transaction(p['tenant']) as session:
        config=company(session,p['tenant']);u=urlsplit(body.url)
        if u.scheme!='https' or u.hostname not in config.source_hosts or u.username or u.password or u.query or u.fragment or u.port not in (None,443):raise HTTPException(422,'仅允许白名单内的HTTPS正文页面。')
        if session.scalar(rows(session,KnowledgeSource,p['tenant']).where(KnowledgeSource.url==body.url)):raise HTTPException(409,'此来源已添加。')
        source=KnowledgeSource(tenant_id=p['tenant'],**body.model_dump());session.add(source);session.flush()
        audit(session,p['tenant'],p['id'],'source.created',source.id)
        return serialize(source)

@app.post('/api/knowledge-sources/{sid}/sync')
def sync_source_now(sid:str,p=Depends(auth)):
    require(p,'admin','editor')
    with transaction(p['tenant']) as session:
        source=locked(session,KnowledgeSource,p['tenant'],sid)
        if not source:raise HTTPException(404,'来源不存在。')
        if source.last_status=='syncing':raise HTTPException(409,'当前正在同步，请稍后查看。')
        source.next_at=time.time();source.active=True;source.last_status='pending'
        audit(session,p['tenant'],p['id'],'source.sync_requested',sid)
    return {'ok':True,'status':'pending'}

@app.get('/api/knowledge-gaps')
def gaps(p=Depends(auth)):
    require(p,'admin','editor','publisher')
    with transaction(p['tenant']) as session:return [serialize(g) for g in session.scalars(rows(session,KnowledgeGap,p['tenant']))]

class GapInput(Input): knowledge_id:str=Field(max_length=40)

@app.post('/api/knowledge-gaps/{gid}/resolve')
def gap_resolve(gid:str,body:GapInput,p=Depends(auth)):
    require(p,'admin','publisher')
    with transaction(p['tenant']) as session:
        gap=locked(session,KnowledgeGap,p['tenant'],gid);k=session.get(Knowledge,(p['tenant'],body.knowledge_id))
        if not gap or not k or k.status!='published':raise HTTPException(409,'请选择本企业已发布知识关联缺口。')
        gap.status='resolved';gap.knowledge_id=k.id;audit(session,p['tenant'],p['id'],'gap.resolved',gid)
    return {'ok':True}

@app.get('/api/offers')
def offers(p=Depends(auth)):
    require(p,'admin','agent')
    with transaction(p['tenant']) as session:return [serialize(o) for o in session.scalars(rows(session,Offer,p['tenant']))]

class OfferInput(Input):
    name:str=Field(min_length=1,max_length=120)
    description:str=Field(max_length=2000)
    tags:list[str]=Field(max_length=20)
    price:float|None=Field(default=None,ge=0,allow_inf_nan=False)
    active:bool=True
    knowledge_id:str|None=Field(default=None,max_length=40)

@app.put('/api/offers/{oid}')
def put_offer(oid:str,body:OfferInput,p=Depends(auth)):
    require(p,'admin')
    with transaction(p['tenant']) as session:
        knowledge=session.get(Knowledge,(p['tenant'],body.knowledge_id)) if body.knowledge_id else None
        if body.active and (not knowledge or knowledge.status!='published' or knowledge.visibility!='public'):raise HTTPException(409,'启用服务需关联已发布对客知识。')
        from .offer_evidence import current_offer_records, supports_public_price
        record=current_offer_records(session,p['tenant'],[body.knowledge_id]).get(body.knowledge_id) if body.knowledge_id else None
        if body.active and not record:raise HTTPException(409,'知识来源已失效或不适用于公开服务，请重新核对。')
        if body.active and body.price is not None and (not knowledge or not record or
                not (knowledge.risk=='high' or record.get('document_id')) or
                not supports_public_price(record,body.name,body.price)):
            raise HTTPException(409,'价格需与已审核来源中的服务名称、完整金额及单位对应；条件价或周期价请先核对，不能直接作为统一公开价。')
        offer=session.get(Offer,(p['tenant'],oid))
        if not offer:offer=Offer(tenant_id=p['tenant'],id=uid());session.add(offer)
        for k,v in body.model_dump().items():setattr(offer,k,v)
        session.flush()
        from .knowledge_provenance import evidence_summary
        audit(session,p['tenant'],p['id'],'offer.updated',offer.id,
              {'price_source':evidence_summary(record),'price_validation':'explicit-flat-cny-v1'} if body.active and body.price is not None and record else {})
        return serialize(offer)

def public_model(row):
    cfg={**model_gateway.DEFAULT,**row.config}
    return {**cfg,'has_key':bool(row.cipher),'api_key':None}

@app.get('/api/models')
def models(p=Depends(auth)):
    require(p,'admin')
    with transaction(p['tenant']) as session:return public_model(session.get(ModelConfig,(p['tenant'],'primary')))

@app.put('/api/models')
def put_model(body:model_gateway.ModelInput,p=Depends(auth)):
    require(p,'admin')
    if body.protocol not in model_gateway.PROTOCOLS or body.token_parameter not in ('max_tokens','max_completion_tokens') or body.system_role not in ('system','developer'):raise HTTPException(422,'模型协议或兼容参数无效。')
    cfg=body.model_dump(exclude={'api_key'});cfg['base_url']=model_gateway.validate_url(body.base_url.strip());cfg['model']=cfg['model'].strip()
    if cfg['model'] and not re.fullmatch(r'[\w./:@-]+',cfg['model']):raise HTTPException(422,'模型名称格式不正确。')
    model_gateway.validate_model(cfg)
    with transaction(p['tenant']) as session:
        row=locked(session,ModelConfig,p['tenant'],'primary')
        old={**model_gateway.DEFAULT,**row.config}
        changed=(old['base_url'],old['protocol'])!=(cfg['base_url'],cfg['protocol'])
        cipher=model_gateway.protect(body.api_key.strip()) if body.api_key.strip() else ('' if changed else row.cipher)
        same=model_gateway.fingerprint(old)==model_gateway.fingerprint(cfg) and not body.api_key.strip() and not changed
        tested=old.get('tested_fingerprint') if same else None
        if cfg['enabled'] and (not cipher or not cfg['model'] or tested!=model_gateway.fingerprint(cfg)):raise HTTPException(409,'请先保存配置并测试成功，再启用模型。')
        cfg.update(tested_fingerprint=tested,tested_at=old.get('tested_at') if same else None,last_test='success' if tested else 'not_tested')
        row.config=cfg;row.cipher=cipher
        audit(session,p['tenant'],p['id'],'model.configured')
        return public_model(row)

@app.post('/api/models/test')
def test_model(p=Depends(auth)):
    require(p,'admin')
    with transaction(p['tenant']) as session:
        row=session.get(ModelConfig,(p['tenant'],'primary'));cfg={**model_gateway.DEFAULT,**row.config};cipher=row.cipher
    if not cipher or not cfg['model']:raise HTTPException(409,'请先填写模型和密钥。')
    fingerprint=model_gateway.fingerprint(cfg);cfg['api_key']=model_gateway.protect(cipher,decrypt=True)
    start=time.monotonic()
    try:model_gateway.complete(cfg,[{'role':'user','content':'请只回复：连接正常。'}])
    except model_gateway.ProviderError as exc:raise HTTPException(502,str(exc)) from None
    with transaction(p['tenant']) as session:
        row=locked(session,ModelConfig,p['tenant'],'primary')
        if row.cipher!=cipher or model_gateway.fingerprint({**model_gateway.DEFAULT,**row.config})!=fingerprint:raise HTTPException(409,'测试期间配置已变化，请重试。')
        row.config={**cfg,'tested_fingerprint':fingerprint,'tested_at':time.time(),'last_test':'success'}
        row.config={k:v for k,v in row.config.items() if k!='api_key'}
        audit(session,p['tenant'],p['id'],'model.tested')
    return {'ok':True,'latency_ms':round((time.monotonic()-start)*1000)}

@app.delete('/api/models/key')
def remove_model_key(p=Depends(auth)):
    require(p,'admin')
    with transaction(p['tenant']) as session:
        row=locked(session,ModelConfig,p['tenant'],'primary');row.cipher='';row.config={**row.config,'enabled':False,'tested_fingerprint':None,'last_test':'not_tested'}
        audit(session,p['tenant'],p['id'],'model.key_removed')
    return {'ok':True}

@app.get('/api/dashboard')
def dashboard(start:float=0,end:float=0,p=Depends(auth)):
    require(p,'admin','agent');end=end or time.time()+1
    if start<0 or end<=start:raise HTTPException(422,'日期范围无效。')
    tid=p['tenant']
    with transaction(tid) as session:
        cs=session.scalars(rows(session,Conversation,tid).where(Conversation.created_at>=start,Conversation.created_at<end)).all()
        tasks=session.scalars(rows(session,Task,tid).where(Task.accepted_at>=start,Task.accepted_at<end)).all()
        due=[t for t in tasks if t.mode=='immediate' and t.deadline_at and t.deadline_at<=time.time() and t.sla!='cancelled']
        met=sum(bool(t.attempted_at and (t.start_at or t.accepted_at)<=t.attempted_at<=t.deadline_at) for t in due)
        scheduled_due=[t for t in tasks if t.mode=='scheduled' and t.deadline_at and t.deadline_at<=time.time() and t.sla!='cancelled']
        scheduled_met=sum(bool(t.attempted_at and (t.start_at or t.accepted_at)<=t.attempted_at<=t.deadline_at) for t in scheduled_due)
        usage=session.scalars(rows(session,Usage,tid).where(Usage.created_at>=start,Usage.created_at<end)).all()
        pending=session.scalar(select(func.count()).select_from(Task).where(Task.tenant_id==tid,Task.status=='pending'))
        return {'conversations':len(cs),'leads':sum(t.deadline_at is not None for t in tasks),'pending':pending,
                'due':len(due),'met':met,'rate':round(met/len(due)*100,1) if due else None,
                'excluded':sum(t.sla=='cancelled' for t in tasks),'breached':sum(t.sla=='breached' for t in tasks),
                'attempted':sum(t.attempted_at is not None for t in tasks),'reached':sum(t.reached_at is not None for t in tasks),
                'immediate':sum(t.mode=='immediate' for t in tasks),'scheduled':sum(t.mode=='scheduled' for t in tasks),
                'scheduled_due':len(scheduled_due),'scheduled_met':scheduled_met,'scheduled_rate':round(scheduled_met/len(scheduled_due)*100,1) if scheduled_due else None,
                'llm_calls':sum(u.kind=='llm' for u in usage),'reserved_tokens':sum(u.tokens for u in usage),
                'usage_basis':'保守预留token估算，不是服务商账单','worker':health()['worker_ready']}

@app.get('/api/export.csv')
def export(start:float=0,end:float=0,p=Depends(auth)):
    require(p,'admin');end=end or time.time()+1
    if start<0 or end<=start:raise HTTPException(422,'日期范围无效。')
    result=io.StringIO();writer=csv.writer(result);writer.writerow(['任务编号','类型','接受时间','联系时限','状态','SLA','人工联系','用户回应'])
    with transaction(p['tenant']) as session:
        for task in session.scalars(rows(session,Task,p['tenant']).where(Task.accepted_at>=start,Task.accepted_at<end).order_by(Task.accepted_at)):
            writer.writerow([task.id,task.mode,task.accepted_at,task.deadline_at,task.status,task.sla,task.attempted_at,task.reached_at])
        audit(session,p['tenant'],p['id'],'report.exported')
    return Response('\ufeff'+result.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="salescare-report.csv"'})

@app.get('/api/audit')
def audits(p=Depends(auth)):
    require(p,'admin','publisher')
    with transaction(p['tenant']) as session:
        q=rows(session,Audit,p['tenant'])
        if 'admin' not in p['roles']:q=q.where(Audit.action.like('knowledge.%'))
        return [serialize(a) for a in session.scalars(q.order_by(Audit.created_at.desc()).limit(100))]

@app.get('/api/setup/local-token')
def local_setup_token(request:Request):
    if request.client.host not in ('127.0.0.1','::1','testclient'):raise HTTPException(403,'仅本机允许初始化。')
    if not setup_status()['required']:raise HTTPException(409,'初始化已完成。')
    return {'token':settings().get('setup_token','')}

@app.get('/sdk/v1/salescare.js')
def sdk():
    return FileResponse(ROOT/'mvp/sdk.js',media_type='application/javascript')

@app.get('/widget.js')
def widget(request:Request):
    origin=settings()['public_origin']
    script="""(()=>{const s=document.currentScript;const slug=s.dataset.company;if(!/^[a-z0-9-]+$/.test(slug))return;
const host=ORIGIN;const b=document.createElement('button');b.textContent='咨询我们';b.setAttribute('aria-label','打开智能客服');
Object.assign(b.style,{position:'fixed',right:'24px',bottom:'24px',background:'#167461',color:'#fff',border:0,borderRadius:'28px',padding:'14px 22px',cursor:'pointer',zIndex:2147483000});
const f=document.createElement('iframe');f.src=host+'/chat/'+slug+'?widget=1';f.title='企业智能客服';f.hidden=true;
Object.assign(f.style,{position:'fixed',right:'20px',bottom:'84px',width:'min(420px,calc(100vw - 32px))',height:'min(700px,calc(100vh - 110px))',border:'1px solid #dbe5e2',borderRadius:'18px',boxShadow:'0 12px 40px #243c3820',zIndex:2147483000,background:'#fff'});
b.onclick=()=>{f.hidden=!f.hidden;b.textContent=f.hidden?'咨询我们':'收起咨询'};document.body.append(f,b);
window.addEventListener('message',e=>{if(e.origin===host&&e.source===f.contentWindow&&e.data&&e.data.type==='salescare.close')f.hidden=true});})();""".replace('ORIGIN',json.dumps(origin))
    return Response(script,media_type='application/javascript')

DIST=ROOT/'web/mvp-dist'
if (DIST/'assets').exists():app.mount('/assets',StaticFiles(directory=DIST/'assets'),name='mvp-assets')

@app.get('/{path:path}')
def frontend(path:str):
    if path.startswith('api/'):raise HTTPException(404,'接口不存在。')
    if not (DIST/'index.html').exists():raise HTTPException(503,'MVP前端尚未构建。')
    return FileResponse(DIST/'index.html')

