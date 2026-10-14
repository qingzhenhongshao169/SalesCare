"""Pilot platform operator UI is local-only and separate from tenant administrators."""
import hmac
import secrets
import time
from fastapi import APIRouter,Request,HTTPException,Depends
from pydantic import BaseModel,Field,ConfigDict
from sqlalchemy import select,text
from .db import transaction,serialization_lock,rows
from .config import settings
from .models import Tenant,CompanySettings,ModelConfig,Invitation,Member
from .security import digest
from .service import audit

router=APIRouter()
def local(request):
    mode=settings()['mode']
    if mode=='production' or request.client.host not in ('127.0.0.1','::1','testclient'):raise HTTPException(403,'内网试点的平台运营仅在服务电脑本机开放。')

def operator(request:Request):
    local(request)
    expected=settings().get('setup_token','')
    if not expected or not hmac.compare_digest(request.headers.get('X-Operator-Token',''),expected):raise HTTPException(403,'平台运营凭证无效。')

@router.get('/api/platform/local-token')
def local_token(request:Request):
    local(request);return {'token':settings().get('setup_token','')}

@router.get('/api/platform/tenants')
def tenants(_=Depends(operator)):
    with transaction() as s:return [{'slug':t.slug,'name':t.name,'active':t.active} for t in s.scalars(select(Tenant).order_by(Tenant.name))]

class NewTenant(BaseModel):
    model_config=ConfigDict(extra='forbid')
    slug:str=Field(pattern='^[a-z0-9-]{3,80}$')
    name:str=Field(min_length=1,max_length=120)
    username:str=Field(pattern='^[a-z0-9._-]{3,80}$')
    admin_name:str=Field(min_length=1,max_length=120)

@router.post('/api/platform/tenants')
def create_tenant(body:NewTenant,_=Depends(operator)):
    token=secrets.token_urlsafe(40)
    with transaction() as s:
        serialization_lock(s,'platform','tenant:'+body.slug)
        if s.scalar(select(Tenant).where(Tenant.slug==body.slug)):raise HTTPException(409,'企业标识已存在。')
        t=Tenant(slug=body.slug,name=body.name);s.add(t);s.flush()
        s.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),{'tenant':t.id})
        s.add(CompanySettings(tenant_id=t.id,id='primary',source_hosts=[],required_skill='专业咨询',service_mode='general_support'))
        s.add(ModelConfig(tenant_id=t.id,id='primary',config={}))
        s.add(Invitation(tenant_id=t.id,username=body.username,name=body.admin_name,roles=['admin','editor','publisher'],token_hash=digest(token),expires_at=time.time()+86400))
        audit(s,t.id,'local-operator','tenant.created',t.id)
    return {'slug':body.slug,'invite_url':settings()['public_origin']+'/invite/'+body.slug+'#'+token,'expires_hours':24}

@router.post('/api/platform/tenants/{slug}/admin-invitation')
def reissue_admin_invitation(slug:str,_=Depends(operator)):
    with transaction() as s:t=s.scalar(select(Tenant).where(Tenant.slug==slug,Tenant.active==True))
    if not t:raise HTTPException(404,'企业不存在。')
    token=secrets.token_urlsafe(40)
    with transaction(t.id) as s:
        serialization_lock(s,t.id,'initial-admin-invite')
        candidates=[r for r in s.scalars(rows(s,Invitation,t.id).order_by(Invitation.created_at)) if 'admin' in r.roles]
        if not candidates:raise HTTPException(409,'此企业没有待开通管理员。')
        previous=candidates[0]
        if s.scalar(rows(s,Member,t.id).where(Member.username==previous.username)):raise HTTPException(409,'首位管理员已开通，请使用既有账号登录。')
        for old in candidates:
            if old.username==previous.username and not old.used:old.used=True
        s.add(Invitation(tenant_id=t.id,username=previous.username,name=previous.name,roles=previous.roles,token_hash=digest(token),expires_at=time.time()+86400))
        audit(s,t.id,'local-operator','tenant.admin_invitation_reissued',previous.username)
    return {'slug':slug,'invite_url':settings()['public_origin']+'/invite/'+slug+'#'+token,'expires_hours':24}
