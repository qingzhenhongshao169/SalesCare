"""Durable platform ingress/egress sharing the existing conversation state machine."""
import json
import time
from sqlalchemy import select,exists,func
from sqlalchemy.orm import aliased
from server.model_gateway import protect
from .db import transaction,rows,locked,database_time,serialization_lock
from .models import *
from .security import digest
from .channels import credentials
from .channel_transport import PlatformError,platform_url,send_dingtalk,send_wechat
from .service import add_message,mark_profile,get_task,handoff,audit,withdraw

def route_data(thread):return json.loads(protect(thread.route_cipher,decrypt=True))

def normalize(kind,frame):
    if kind=='dingtalk':
        b=frame.get('data',{})
        if isinstance(b,str):b=json.loads(b)
        sender=b.get('senderStaffId') or b.get('senderId');room=b.get('conversationId');mid=b.get('msgId') or frame.get('headers',{}).get('messageId')
        text=b.get('text',{}).get('content','') if b.get('msgtype')=='text' else '[暂不支持的消息类型]';url=platform_url(b.get('sessionWebhook',''),'oapi.dingtalk.com','/robot/sendBySession')
        route={'sender_id':sender,'room_id':room,'session_webhook':url,'expires_at':float(b.get('sessionWebhookExpiredTime',0))/1000,'group':str(b.get('conversationType',''))=='2'}
    elif kind=='wecom_bot':
        b=frame.get('body',{})
        sender=b.get('from',{}).get('userid');room=b.get('chatid') or sender;mid=b.get('msgid');text=b.get('text',{}).get('content','') if b.get('msgtype')=='text' else '[暂不支持的消息类型]'
        route={'sender_id':sender,'room_id':room,'group':b.get('chattype')=='group','expires_at':time.time()+86400}
    elif kind=='wechat_kf':
        b=frame
        if b.get('origin')!=3:raise PlatformError('unsupported_message')
        sender=b.get('external_userid');room=b.get('open_kfid');mid=b.get('msgid');text=b.get('text',{}).get('content','') if b.get('msgtype')=='text' else '[暂不支持的消息类型]'
        route={'sender_id':sender,'room_id':room,'group':False,'expires_at':time.time()+48*3600,'native_human':bool(b.get('_native_human',False))}
    else:raise PlatformError('invalid_response')
    if any(not isinstance(x,str) or not x or len(x)>500 for x in (sender,room,mid)) or not isinstance(text,str) or not text.strip() or len(text)>2000:raise PlatformError('invalid_response')
    return mid,room,sender,text.strip(),route

def ingest(tid,cid,version,frame,owner=None):
    with transaction(tid) as s:
        ch=locked(s,Channel,tid,cid);now=database_time(s)
        if not ch or not ch.enabled or ch.version!=version or (owner and (ch.lease_owner!=owner or ch.lease_until<=now)):raise PlatformError('channel_disabled')
        mid,room,sender,text,route=normalize(ch.kind,frame)
        if ch.kind=='wechat_kf' and room!=ch.config['open_kf_id']:raise PlatformError('auth')
        serialization_lock(s,tid,'channel-in:'+cid)
        event_key=digest(mid)
        previous=s.scalar(rows(s,ChannelReceipt,tid).where(ChannelReceipt.channel_id==cid,ChannelReceipt.event_key==event_key))
        if previous:return previous.conversation_id,True
        rate=s.scalar(select(func.count()).select_from(ChannelReceipt).where(ChannelReceipt.tenant_id==tid,ChannelReceipt.channel_id==cid,ChannelReceipt.created_at>now-60))
        if rate>=120:raise PlatformError('rate_limited')
        key=digest(json.dumps([ch.identity_key,room,sender]))
        thread=s.scalar(rows(s,ExternalThread,tid).where(ExternalThread.channel_id==cid,ExternalThread.external_key==key).with_for_update())
        c=locked(s,Conversation,tid,thread.conversation_id) if thread else None
        if not c or c.state=='CLOSED':
            c=Conversation(tenant_id=tid,visitor_id='external-'+key[:30],channel=ch.kind,channel_id=cid,name='平台访客');s.add(c);s.flush()
            if thread:thread.conversation_id=c.id
            else:thread=ExternalThread(tenant_id=tid,channel_id=cid,conversation_id=c.id,external_key=key);s.add(thread)
        thread.route_cipher=protect(json.dumps(route));thread.eligible_until=route['expires_at']
        if route.get('native_human'):c.state='HUMAN_ACTIVE'
        elif ch.kind=='wechat_kf' and c.state=='HUMAN_ACTIVE' and not get_task(s,tid,c.id):c.state='AI_ACTIVE'
        s.add(ChannelReceipt(tenant_id=tid,channel_id=cid,event_key=event_key,conversation_id=c.id))
        m=add_message(s,c,'visitor',text,client_id='platform-'+event_key)
        mark_profile(s,c,text);ch.received_at=now
        if text in ('停止联系','不要联系','取消联系'):
            withdraw(s,{'tenant':tid,'id':c.visitor_id},c);return c.id,False
        if route.get('native_human'):return c.id,False
        if text=='[暂不支持的消息类型]':
            if c.state in ('AI_ACTIVE','HANDOFF_PENDING'):add_message(s,c,'ai','目前可以通过文字咨询。请简单描述您的需求，也可以发送“转人工”请专业顾问协助。')
        elif text in ('转人工','人工客服','找人工','联系人工'):
            p={'tenant':tid,'id':c.visitor_id,'kind':'visitor','roles':['visitor']};handoff(s,p,c)
            add_message(s,c,'ai','已为您通知专业顾问，等待接入期间也可以继续咨询。')
        elif c.state in ('AI_ACTIVE','HANDOFF_PENDING'):s.add(AIJob(tenant_id=tid,conversation_id=c.id,message_id=m.id))
        else:
            task=get_task(s,tid,c.id)
            if task and task.attempted_at and task.reached_at is None:task.reached_at=now
        return c.id,False

def queue_message(s,c,m):
    if not c.channel_id or c.channel=='web' or m.role not in ('ai','human'):return
    ch=s.get(Channel,(c.tenant_id,c.channel_id))
    if not ch:return
    task=get_task(s,c.tenant_id,c.id)
    s.add(ChannelDelivery(tenant_id=c.tenant_id,channel_id=ch.id,conversation_id=c.id,message_id=m.id,
                          channel_version=ch.version,task_version=task.version if m.role=='human' and task else 0))

def check_delivery(s,delivery):
    tid=delivery.tenant_id;ch=s.get(Channel,(tid,delivery.channel_id));c=s.get(Conversation,(tid,delivery.conversation_id));m=s.get(Message,(tid,delivery.message_id));task=get_task(s,tid,c.id) if c else None
    if not ch or not ch.enabled or ch.version!=delivery.channel_version or not c or c.state=='CLOSED':raise PlatformError('stale')
    lead=s.scalar(rows(s,Lead,tid).where(Lead.conversation_id==c.id))
    if lead and lead.withdrawn_at:raise PlatformError('stale')
    if m.role=='ai':
        if c.state not in ('AI_ACTIVE','HANDOFF_PENDING'):raise PlatformError('stale')
        from .knowledge_retrieval import validate_citations
        if not validate_citations(s,tid,m.citations,c.channel_id):raise PlatformError('stale')
    if m.role=='human':
        member=s.get(Member,(tid,m.actor_id))
        if not task or task.version!=delivery.task_version or task.status!='claimed' or task.owner_id!=m.actor_id or not member or not member.active or 'agent' not in member.roles or task.skill not in member.skills:raise PlatformError('stale')
    thread=s.scalar(rows(s,ExternalThread,tid).where(ExternalThread.conversation_id==c.id))
    if not thread or not thread.route_cipher or thread.eligible_until<=database_time(s):raise PlatformError('window_expired')
    if route_data(thread).get('native_human'):raise PlatformError('native_human')
    return ch,c,m,thread,task

def prepare_delivery(tid,channel_id=None):
    with transaction(tid) as s:
        now=database_time(s);older=aliased(ChannelDelivery);old_message=aliased(Message)
        unfinished=exists(select(1).select_from(older).join(old_message,(old_message.tenant_id==older.tenant_id)&(old_message.id==older.message_id)).where(older.tenant_id==tid,older.conversation_id==ChannelDelivery.conversation_id,old_message.sequence<Message.sequence,older.status.in_(['pending','retry','sending'])))
        q=rows(s,ChannelDelivery,tid).join(Message,(Message.tenant_id==ChannelDelivery.tenant_id)&(Message.id==ChannelDelivery.message_id)).where(ChannelDelivery.status.in_(['pending','retry','sending']),ChannelDelivery.due_at<=now,ChannelDelivery.lease_until<now,~unfinished)
        if channel_id:q=q.where(ChannelDelivery.channel_id==channel_id)
        d=s.scalar(q.order_by(ChannelDelivery.created_at).with_for_update(of=ChannelDelivery,skip_locked=True).limit(1))
        if not d:return None
        if d.status=='sending':d.status='uncertain';d.last_error='uncertain';return None
        try:ch,c,m,thread,task=check_delivery(s,d)
        except PlatformError as e:d.status='cancelled' if e.code=='stale' else 'failed';d.last_error=e.code;return None
        d.status='sending';d.attempts+=1;d.lease_until=now+30
        return {'id':d.id,'attempts':d.attempts,'lease':d.lease_until,'kind':ch.kind,'version':ch.version,'channel_id':ch.id,'config':dict(ch.config),'credentials':credentials(ch),'route':route_data(thread),'text':m.text,'message_id':m.id}

def complete_delivery(tid,item,error=''):
    with transaction(tid) as s:
        d=locked(s,ChannelDelivery,tid,item['id'])
        if not d or d.status!='sending' or d.attempts!=item['attempts'] or d.lease_until!=item['lease']:return
        now=database_time(s)
        try:ch,c,m,thread,task=check_delivery(s,d)
        except PlatformError as e:
            d.status='accepted_stale' if not error else 'cancelled';d.last_error=e.code;d.lease_until=0
            if not error:
                d.accepted_at=now;audit(s,tid,'channel-worker','delivery.accepted_after_state_change',d.message_id)
            return
        d.lease_until=0;d.last_error=error
        if not error:
            d.status='accepted';d.accepted_at=now;ch.sent_at=now
            if m.role=='human':
                s.add(ContactAttempt(tenant_id=tid,task_id=task.id,message_id=m.id,actor_id=m.actor_id,evidence='platform_api_accepted',created_at=now))
                if task.attempted_at is None:
                    task.attempted_at=now
                    if task.deadline_at:task.sla='met' if now<=task.deadline_at else 'breached'
                audit(s,tid,m.actor_id,'contact.platform_accepted',m.id)
        elif error in ('timeout','network','uncertain'):d.status='uncertain';d.last_error='uncertain'
        elif error in ('rate_limited','connect_failed') and d.attempts<6:d.status='retry';d.due_at=now+min(300,2**d.attempts*5)
        else:d.status='failed'

def deliver_http(tid,cid):
    item=prepare_delivery(tid,cid)
    if not item:return
    error=''
    try:
        with transaction(tid) as s:check_delivery(s,s.get(ChannelDelivery,(tid,item['id'])))
        if item['kind']=='dingtalk':send_dingtalk(item['config'],item['credentials'],item['route'],item['text'])
        elif item['kind']=='wechat_kf':send_wechat(item['config'],item['credentials'],item['route'],item['text'],item['message_id'])
        else:raise PlatformError('invalid_response')
    except PlatformError as e:error=e.code
    except Exception:error='network'
    complete_delivery(tid,item,error)
