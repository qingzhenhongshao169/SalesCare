"""Tenant-owned channel configuration. Public projection never includes credentials."""
import json
import re
import time
from urllib.parse import urlsplit
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select,func
from sqlalchemy.exc import IntegrityError
from server.model_gateway import protect
from .config import settings
from .db import transaction,rows,locked,serialization_lock,database_time
from .models import Channel,Knowledge,uid
from .security import principal,require,digest
from .service import audit,company

def field(key,label,secret=False,required=True,help=''):
    return dict(key=key,label=label,secret=secret,required=required,help=help)

TYPES={
 'web':dict(name='网站 JS SDK',mode='script + iframe',fields=[],doc='',scope='可访问网站的客户',transport='web'),
 'dingtalk':dict(name='钉钉应用机器人',mode='Stream 长连接',fields=[field('client_id','Client ID / AppKey',help='企业内部应用的应用凭证'),field('client_secret','Client Secret / AppSecret',True),field('robot_code','Robot Code',required=False,help='机器人标识；按开发者后台填写')],doc='https://github.com/open-dingtalk/dingtalk-stream-sdk-python',scope='按应用发布、安装及可见范围；群内需 @机器人',transport='stream'),
 'wecom_bot':dict(name='企微智能机器人',mode='WebSocket 长连接',fields=[field('bot_id','Bot ID'),field('secret','Secret',True)],doc='https://github.com/WecomTeam/aibot-node-sdk',scope='按机器人可见范围；不能据此推定普通微信客户可访问',transport='stream'),
 'wechat_kf':dict(name='微信客服',mode='API 回调 + 消息同步',fields=[field('corp_id','企业 ID / CorpID'),field('secret','微信客服 Secret',True,help='使用微信客服权限对应的 Secret，不是公众号密钥'),field('token','回调 Token',True),field('encoding_aes_key','EncodingAESKey',True,help='43 位消息加密密钥'),field('open_kf_id','客服账号 ID / OpenKfID',help='本实例绑定的微信客服账号')],doc='https://developer.work.weixin.qq.com/document/path/94670',scope='微信客服入口的客户；回调需要公网 HTTPS',transport='callback'),
}
COMMON={'knowledge_ids','required_skill','allowed_origins','welcome','button_text','color','position'}
STATUS={'draft':'待验证','verified':'凭据已验证','connecting':'连接中','connected':'已连接','disconnected':'连接中断','failed':'验证或连接失败','disabled':'已暂停','await_public_https':'等待公网 HTTPS 回调'}
ERRORS_NATIVE={'native_human':'当前由平台原生人工接待，已停止机器人发送'}
ERRORS={'auth':'凭据或应用权限无效','network':'网络连接失败','timeout':'连接超时','provider':'平台拒绝请求','invalid_response':'平台返回格式不受支持','config_changed':'配置已变化，请重新检测','lease_lost':'连接归属已变化','callback_unverified':'回调尚未验证','unsupported_message':'目前支持文本消息','window_expired':'平台回复资格已过期','uncertain':'发送结果未确认，需核对平台后处理','credentials_missing':'请填写必填凭据','channel_disabled':'渠道已暂停','stale':'消息或任务已失效','rate_limited':'平台限流'}
ERRORS.update(ERRORS_NATIVE)
ERRORS['connect_failed']='未能建立连接，系统将按策略重试'

class ChannelInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=120)
    kind:str=Field(max_length=30)
    config:dict=Field(default_factory=dict)
    credentials:dict[str,str]=Field(default_factory=dict)
    version:int=Field(default=0,ge=0)

class VersionInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    version:int=Field(ge=1)

def auth(request:Request):return principal(request,write=request.method not in ('GET','HEAD','OPTIONS'))
router=APIRouter()

def credentials(ch):
    return json.loads(protect(ch.cipher,decrypt=True)) if ch.cipher else {}

def channel_view(ch,slug):
    cfg=ch.config or {};saved=credentials(ch)
    status=ch.status
    if ch.enabled and TYPES[ch.kind]['transport']=='stream' and status=='connected' and ch.heartbeat_at<time.time()-90:status='disconnected'
    return {'id':ch.id,'name':ch.name,'kind':ch.kind,'config':cfg,'version':ch.version,'enabled':ch.enabled,
            'status':status,'status_label':STATUS.get(status,status),'diagnostic':ERRORS.get(ch.diagnostic,''),
            'tested_at':ch.tested_at,'tested':ch.tested_version==ch.version,'heartbeat_at':ch.heartbeat_at,
            'received_at':ch.received_at,'sent_at':ch.sent_at,'configured_fields':[f['key'] for f in TYPES[ch.kind]['fields'] if saved.get(f['key']) or cfg.get(f['key'])],
            'callback_url':settings()['public_origin']+'/api/channel-callback/'+slug+'/'+ch.id if ch.kind=='wechat_kf' else '',
            'embed_code':f'<script src="{settings()["public_origin"]}/sdk/v1/salescare.js" data-company="{slug}" data-channel="{ch.id}" defer></script>' if ch.kind=='web' else '',
            'message_scope':TYPES[ch.kind]['scope'],'model_binding':'企业当前模型','receipts_verified':bool(ch.received_at and ch.sent_at)}

def validate_input(body,old=None):
    if not body.name.strip():raise HTTPException(422,'请填写渠道名称。')
    if body.kind not in TYPES:raise HTTPException(422,'请选择受支持的渠道类型。')
    if old and old.kind!=body.kind:raise HTTPException(409,'渠道类型不能修改，请新增渠道。')
    fields=TYPES[body.kind]['fields'];public_keys={f['key'] for f in fields if not f['secret']}
    secret_keys={f['key'] for f in fields if f['secret']}
    if set(body.config)-COMMON-public_keys or set(body.credentials)-secret_keys:raise HTTPException(422,'配置字段与接入模式不匹配。')
    cfg=dict(body.config);saved=credentials(old) if old else {}
    for key,value in body.credentials.items():
        if len(value)>1000:raise HTTPException(422,'凭据长度超过限制。')
        if value.strip():saved[key]=value.strip()
    if old and any(old.config.get(k)!=cfg.get(k) for k in public_keys):
        if any(not body.credentials.get(k,'').strip() for k in secret_keys):raise HTTPException(422,'更换应用或账号标识时请重新填写相应密钥。')
    for f in fields:
        value=saved.get(f['key']) if f['secret'] else cfg.get(f['key'])
        if f['required'] and (not isinstance(value,str) or not value.strip()):raise HTTPException(422,'请填写：'+f['label'])
        if value and (not isinstance(value,str) or len(value)>300):raise HTTPException(422,'字段长度或格式不正确。')
    if body.kind=='wechat_kf' and not re.fullmatch('[A-Za-z0-9+/]{43}',saved.get('encoding_aes_key','')):raise HTTPException(422,'EncodingAESKey 应为43位。')
    if not isinstance(cfg.get('knowledge_ids',[]),list) or len(cfg.get('knowledge_ids',[]))>300 or any(not isinstance(k,str) or len(k)>40 for k in cfg.get('knowledge_ids',[])):raise HTTPException(422,'知识范围无效。')
    skill=cfg.get('required_skill','')
    if not isinstance(skill,str) or len(skill)>120:raise HTTPException(422,'接待技能无效。')
    origins=cfg.get('allowed_origins',[])
    if not isinstance(origins,list) or len(origins)>20:raise HTTPException(422,'网站域名数量无效。')
    clean=[]
    for origin in origins:
        if not isinstance(origin,str):raise HTTPException(422,'网站源地址无效。')
        origin=origin.strip()
        if not origin:continue
        try:u=urlsplit(origin);port=u.port;host=u.hostname.encode('idna').decode() if u.hostname else ''
        except (ValueError,UnicodeError):raise HTTPException(422,'网站源地址无效。') from None
        if u.scheme not in ('http','https') or not host or not re.fullmatch('[a-zA-Z0-9.:-]+',host) or u.username or u.password or u.path not in ('','/') or u.query or u.fragment or (settings()['mode']=='production' and u.scheme!='https'):raise HTTPException(422,'请填写完整网站源地址，例如 https://www.example.com。')
        normalized=u.scheme+'://'+('['+host+']' if ':' in host else host)+((':'+str(port)) if port and port!={'http':80,'https':443}[u.scheme] else '')
        clean.append(normalized)
    cfg['allowed_origins']=clean
    if body.kind=='web' and not clean:raise HTTPException(422,'请至少配置一个允许嵌入的网站源地址。')
    if not isinstance(cfg.get('color','#167461'),str) or not re.fullmatch('#[A-Fa-f0-9]{6}',cfg.get('color','#167461')):raise HTTPException(422,'主题颜色格式不正确。')
    if cfg.get('position','right') not in ('right','left'):raise HTTPException(422,'显示位置无效。')
    for key,maximum in (('welcome',500),('button_text',30)):
        if not isinstance(cfg.get(key,''),str) or len(cfg.get(key,''))>maximum:raise HTTPException(422,'欢迎语或入口文字过长。')
    identity=None
    if body.kind!='web':
        keys={'dingtalk':['client_id'],'wecom_bot':['bot_id'],'wechat_kf':['corp_id','open_kf_id']}[body.kind]
        identity=digest(body.kind+':'+':'.join(cfg[k].strip() for k in keys))
    return cfg,saved,identity

@router.get('/api/channel-types')
def types(p=Depends(auth)):
    require(p,'admin');return [{'kind':kind,**value} for kind,value in TYPES.items()]

@router.get('/api/channels')
def listing(p=Depends(auth)):
    require(p,'admin')
    with transaction(p['tenant']) as s:return [channel_view(ch,p['slug']) for ch in s.scalars(rows(s,Channel,p['tenant']).order_by(Channel.created_at))]

@router.post('/api/channels')
def create(body:ChannelInput,p=Depends(auth)):
    return save(None,body,p)

@router.put('/api/channels/{cid}')
def update(cid:str,body:ChannelInput,p=Depends(auth)):
    return save(cid,body,p)

def save(cid,body,p):
    require(p,'admin');tid=p['tenant']
    try:
        with transaction(tid) as s:
            serialization_lock(s,tid,'channels')
            ch=locked(s,Channel,tid,cid) if cid else None
            if cid and not ch:raise HTTPException(404,'渠道不存在。')
            if ch and body.version!=ch.version:raise HTTPException(409,'配置已变化，请刷新后再保存。')
            if not ch and s.scalar(select(func.count()).select_from(Channel).where(Channel.tenant_id==tid))>=20:raise HTTPException(409,'当前试点最多20个渠道实例。')
            cfg,saved,identity=validate_input(body,ch)
            ids=cfg.get('knowledge_ids',[])
            if ids and set(ids)-set(s.scalars(select(Knowledge.id).where(Knowledge.tenant_id==tid,Knowledge.id.in_(ids)))):raise HTTPException(422,'知识范围包含不存在或不属于本企业的知识。')
            if not ch:ch=Channel(tenant_id=tid,kind=body.kind,name=body.name);s.add(ch)
            else:ch.version+=1
            ch.name=body.name;ch.config=cfg;ch.cipher=protect(json.dumps(saved)) if saved else '';ch.identity_key=identity
            ch.enabled=False;ch.tested_version=0;ch.status='draft';ch.diagnostic='';ch.lease_until=0;ch.lease_owner='';ch.heartbeat_at=0
            ch.runtime_cipher='';ch.poll_due=0
            s.flush();audit(s,tid,p['id'],'channel.saved',ch.id,{'kind':ch.kind,'version':ch.version})
            return channel_view(ch,p['slug'])
    except IntegrityError:raise HTTPException(409,'此平台应用或机器人已绑定，请核对已有渠道或联系平台负责人。') from None

@router.post('/api/channels/{cid}/test')
def test(cid:str,body:VersionInput,p=Depends(auth)):
    require(p,'admin');tid=p['tenant']
    with transaction(tid) as s:
        ch=s.get(Channel,(tid,cid))
        if not ch:raise HTTPException(404,'渠道不存在。')
        if ch.enabled:raise HTTPException(409,'请先暂停渠道再检测，避免测试连接干扰运行中的机器人。')
        if body.version!=ch.version:raise HTTPException(409,'请刷新最新配置。')
        kind,cfg,saved,version=ch.kind,dict(ch.config),credentials(ch),ch.version
    from .channel_transport import check_credentials
    success,error=check_credentials(kind,cfg,saved)
    with transaction(tid) as s:
        ch=locked(s,Channel,tid,cid)
        if ch.version!=version:raise HTTPException(409,'测试期间配置已变化，请重测。')
        ch.diagnostic=error;ch.status='verified' if success else 'failed';ch.tested_version=version if success else 0;ch.tested_at=database_time(s)
        if ch.enabled and not success:ch.enabled=False;ch.lease_until=0
        audit(s,tid,p['id'],'channel.tested',cid,{'success':success,'error':error})
        return channel_view(ch,p['slug'])

@router.post('/api/channels/{cid}/{action}')
def action(cid:str,action:str,body:VersionInput,p=Depends(auth)):
    require(p,'admin')
    if action not in ('enable','disable','unbind'):raise HTTPException(404,'操作不存在。')
    with transaction(p['tenant']) as s:
        ch=locked(s,Channel,p['tenant'],cid)
        if not ch:raise HTTPException(404,'渠道不存在。')
        if body.version!=ch.version:raise HTTPException(409,'配置已变化，请刷新。')
        if action=='enable':
            if ch.tested_version!=ch.version:raise HTTPException(409,'请先完成当前配置的连接检测。')
            if not company(s,p['tenant']).published:raise HTTPException(409,'请先在企业配置中发布客户入口。')
            if ch.kind=='wechat_kf' and not settings()['public_origin'].startswith('https://'):raise HTTPException(409,'微信回调需要公网 HTTPS；当前内网可保存配置及检测凭据。')
            if ch.kind=='wechat_kf' and not (json.loads(protect(ch.runtime_cipher,decrypt=True)) if ch.runtime_cipher else {}).get('callback_verified'):raise HTTPException(409,'请先在微信客服后台完成回调地址验证。')
            ch.enabled=True;ch.status='connected' if ch.kind=='web' else 'connecting';ch.diagnostic=''
        else:ch.enabled=False;ch.version+=1;ch.tested_version=0;ch.status='disabled';ch.diagnostic='';ch.lease_until=0;ch.lease_owner=''
        if action=='unbind':
            ch.cipher='';ch.runtime_cipher='';ch.identity_key=None;ch.config={k:v for k,v in ch.config.items() if k in COMMON}
            from .models import ExternalThread
            for thread in s.scalars(rows(s,ExternalThread,p['tenant']).where(ExternalThread.channel_id==cid)):thread.route_cipher='';thread.eligible_until=0
        audit(s,p['tenant'],p['id'],'channel.'+action,cid)
        return channel_view(ch,p['slug'])

@router.get('/api/channels/{cid}/deliveries')
def delivery_list(cid:str,p=Depends(auth)):
    require(p,'admin')
    from .models import ChannelDelivery
    with transaction(p['tenant']) as s:
        if not s.get(Channel,(p['tenant'],cid)):raise HTTPException(404,'渠道不存在。')
        return [{'id':d.id,'conversation_id':d.conversation_id,'message_id':d.message_id,'status':d.status,'attempts':d.attempts,'error':ERRORS.get(d.last_error,''),'accepted_at':d.accepted_at} for d in s.scalars(rows(s,ChannelDelivery,p['tenant']).where(ChannelDelivery.channel_id==cid).order_by(ChannelDelivery.created_at.desc()).limit(30))]

def channel_for_session(s,p):
    if not p.get('channel_id'):return None
    ch=s.get(Channel,(p['tenant'],p['channel_id']))
    if not ch or ch.kind!='web' or not ch.enabled:raise HTTPException(409,'此网站渠道已暂停。')
    return ch
