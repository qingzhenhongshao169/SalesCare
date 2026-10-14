"""Authenticated WeChat callbacks. LAN configuration is allowed; production requires HTTPS."""
import json
from fastapi import APIRouter,Request,HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from server.model_gateway import protect
from .db import transaction,locked
from .models import Tenant,Channel
from .channels import credentials
from .channel_transport import parse_xml,decrypt_callback,PlatformError

router=APIRouter()
def find_channel(slug,cid):
    with transaction() as s:t=s.scalar(select(Tenant).where(Tenant.slug==slug,Tenant.active==True))
    if not t:raise HTTPException(404,'渠道不存在。')
    with transaction(t.id) as s:
        ch=s.get(Channel,(t.id,cid))
        if not ch or ch.kind!='wechat_kf':raise HTTPException(404,'渠道不存在。')
        return t.id,ch.version,dict(ch.config),credentials(ch)

@router.api_route('/api/channel-callback/{slug}/{cid}',methods=['GET','POST'])
async def callback(slug:str,cid:str,request:Request):
    tid,version,cfg,saved=find_channel(slug,cid)
    q=request.query_params
    raw=b''
    if request.method=='POST':
        async for part in request.stream():
            raw+=part
            if len(raw)>64000:raise HTTPException(413,'回调大小超过限制。')
    try:
        encrypted=q.get('echostr','') if request.method=='GET' else parse_xml(raw).get('Encrypt','')
        plain=decrypt_callback(saved,cfg['corp_id'],encrypted,q.get('msg_signature',''),q.get('timestamp',''),q.get('nonce',''))
        event=parse_xml(plain) if request.method=='POST' else None
    except PlatformError:raise HTTPException(403,'回调认证失败。') from None
    with transaction(tid) as s:
        ch=locked(s,Channel,tid,cid)
        if ch.version!=version:raise HTTPException(409,'配置已变化。')
        state=json.loads(protect(ch.runtime_cipher,decrypt=True)) if ch.runtime_cipher else {}
        if request.method=='GET':state['callback_verified']=True
        else:
            if not ch.enabled:raise HTTPException(409,'渠道未启用。')
            if event.get('Event')!='kf_msg_or_event' or event.get('OpenKfId')!=cfg['open_kf_id']:return Response('success',media_type='text/plain')
            state['sync_token']=event.get('Token','');ch.poll_due=0
        ch.runtime_cipher=protect(json.dumps(state))
    return Response(plain if request.method=='GET' else 'success',media_type='text/plain')
