import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from sqlalchemy import select, func
from server.model_gateway import protect
from server.domain import company_profile
from .db import rows, locked, database_time, serialization_lock
from .models import *
from .security import serialize
from .reception import task_presentation,handoff_summary

CONSENT = '我同意本企业的专业顾问就本次需求通过当前会话联系我；不包括营销推送，可随时撤回。'

def audit(session, tid, actor, action, target='', detail=None):
    session.add(Audit(tenant_id=tid,actor=actor,action=action,target=target,detail=detail or {}))

def company(session, tid):
    result = session.get(CompanySettings,(tid,'primary'))
    if not result: raise HTTPException(404,'企业配置不存在。')
    return result

def get_conversation(session,p,cid,for_update=False):
    q=rows(session,Conversation,p['tenant']).where(Conversation.id==cid)
    if for_update: q=q.with_for_update()
    c=session.scalar(q)
    if not c or (p['kind']=='visitor' and c.visitor_id!=p['id']):
        raise HTTPException(404,'会话不存在或无权访问。')
    if p['kind']=='employee' and not set(p['roles']).intersection({'admin','agent'}):
        raise HTTPException(403,'当前账号没有会话访问权限。')
    return c

def add_message(session,c,role,text,actor_id=None,actor_name='',client_id=None,citations=None):
    c.sequence+=1
    message=Message(tenant_id=c.tenant_id,conversation_id=c.id,sequence=c.sequence,role=role,text=text,
                    actor_id=actor_id,actor_name=actor_name,client_id=client_id,citations=citations or [])
    session.add(message);session.flush()
    if c.channel_id and c.channel!='web':
        from .channel_messages import queue_message
        queue_message(session,c,message)
    elif c.channel_id:
        ch=session.get(Channel,(c.tenant_id,c.channel_id))
        if ch:
            if role=='visitor':ch.received_at=database_time(session)
            elif role in ('ai','human'):ch.sent_at=database_time(session)
    return message

def get_task(session,tid,cid):
    return session.scalar(rows(session,Task,tid).where(Task.conversation_id==cid))

def in_hours(config, when):
    d=datetime.fromtimestamp(when,ZoneInfo(config.timezone))
    return d.weekday() in config.weekdays and config.opens<=d.strftime('%H:%M')<config.closes

def appointment_slots(config,now):
    start=datetime.fromtimestamp(now,ZoneInfo(config.timezone))
    choices=[]
    for n in range(8):
        day=start+timedelta(days=n)
        if day.weekday() not in config.weekdays: continue
        for hour in (config.opens,'14:00'):
            if not config.opens<=hour<config.closes: continue
            h,m=map(int,hour.split(':'))
            dt=day.replace(hour=h,minute=m,second=0,microsecond=0)
            if dt.timestamp()>now+300: choices.append({'at':dt.timestamp(),'label':dt.strftime('%m月%d日 %H:%M')})
    return choices[:8]

def candidates(session,tid,skill,now,live=True):
    members=session.scalars(rows(session,Member,tid).where(Member.active==True).order_by(Member.id)).all()
    result=[]
    for m in members:
        if 'agent' not in m.roles or skill not in m.skills: continue
        load=session.scalar(select(func.count()).select_from(Task).where(Task.tenant_id==tid,Task.owner_id==m.id,Task.status=='claimed'))
        if load>=m.capacity: continue
        if live and (not m.available or m.heartbeat_at<now-90): continue
        result.append((load,m))
    return [m for _,m in sorted(result,key=lambda x:(x[0],x[1].id))]

def check_ready(session,tid):
    config=company(session,tid)
    has_knowledge=bool(session.scalar(rows(session,Knowledge,tid).where(Knowledge.status=='published',Knowledge.visibility=='public').limit(1)))
    qualified=bool(candidates(session,tid,config.required_skill,time.time(),live=False))
    return {'knowledge':has_knowledge,'qualified_agent':qualified,'published':config.published,
            'notification':config.notification_kind!='none' and bool(config.notification_cipher),
            'website_ready':has_knowledge,'immediate_ready':qualified and in_hours(config,time.time()) and bool(candidates(session,tid,config.required_skill,time.time()))}

def mark_profile(session,c,text):
    offers=session.scalars(rows(session,Offer,c.tenant_id).where(Offer.active==True)).all()
    knowledge=session.scalars(rows(session,Knowledge,c.tenant_id).where(Knowledge.status=='published',Knowledge.visibility=='public')).all()
    d={'offers':[{'id':o.id,'tags':o.tags,'active':o.active,'source_url':o.knowledge_id} for o in offers],
       'knowledge':[{'status':k.status,'visibility':k.visibility,'channels':['web'],'expires_at':k.expires_at,'source_url':k.id} for k in knowledge]}
    data={'profile':dict(c.profile),'channel':'web'}
    if company(session,c.tenant_id).service_mode=='general_support':
        from .answering import social_intent
        if not social_intent(text) and not re.fullmatch(r'(?:请)?转人工[。！!]*',text.strip()):
            clean=redacted_text(text)
            data['profile']['need']={'label':'咨询内容','value':clean[:140],'display':clean[:140],'evidence':clean,'source':'用户原话','confirmed':True}
    else:company_profile(d,data,text)
    # Keep the latest explicit statement, skipping negations, questions and ranges.
    original=dict(c.profile)
    def affirmative(match):
        prefix=text[max(0,match.start()-5):match.start()]
        suffix=text[match.end():match.end()+6]
        return not re.search(r'不是|并非|没有|不超过|至少|超过|不到',prefix) and not re.match(r'\s*[-–~到至]\s*\d',suffix)
    people=[m for m in re.finditer(r'(\d+)\s*人',text) if affirmative(m)]
    budget=[m for m in re.finditer(r'预算(?:上限|是|为|改为|改成|只有|约|大概)?\s*(\d+(?:\.\d+)?)\s*(万|千)?',text) if affirmative(m)]
    if people:data['profile']['people']={'label':'团队规模','value':int(people[-1][1]),'display':people[-1][1]+' 人','evidence':text,'source':'用户原话','confirmed':True}
    elif 'people' in original:data['profile']['people']=original['people']
    else:data['profile'].pop('people',None)
    if budget:
        value=float(budget[-1][1])*{None:1,'千':1000,'万':10000}[budget[-1][2]]
        data['profile']['budget']={'label':'预算范围','value':value,'display':f'¥{value:,.0f}','evidence':text,'source':'用户原话','confirmed':True}
    elif 'budget' in original:data['profile']['budget']=original['budget']
    else:data['profile'].pop('budget',None)
    timeline=re.search(r'(?:计划|希望|期望|预计).{0,6}(下周|本月|下个月|\d{4}年\d{1,2}月\d{1,2}日)',text)
    if timeline:data['profile']['timeline']={'label':'计划时间','value':timeline[1],'display':timeline[1],'evidence':text,'source':'用户原话','confirmed':True}
    c.profile=data['profile']

def conversation_view(session,p,c):
    out=serialize(c)
    task=get_task(session,p['tenant'],c.id)
    member=session.get(Member,(p['tenant'],p['id'])) if p['kind']=='employee' else None
    pii_allowed=bool(member and 'agent' in member.roles and task and task.owner_id==p['id'] and task.status=='claimed' and task.skill in member.skills)
    messages=session.scalars(rows(session,Message,p['tenant']).where(Message.conversation_id==c.id).order_by(Message.sequence)).all()
    out['messages']=[serialize(m) for m in messages]
    if c.channel_id:
        channel=session.get(Channel,(p['tenant'],c.channel_id))
        out['channel_name']=channel.name if channel else '渠道已停用'
        if p['kind']=='employee':
            deliveries={d.message_id:d for d in session.scalars(rows(session,ChannelDelivery,p['tenant']).where(ChannelDelivery.conversation_id==c.id))}
            for message in out['messages']:
                delivery=deliveries.get(message['id'])
                if delivery:message['delivery_status']=delivery.status;message['delivery_error']=delivery.last_error
    if p['kind']=='employee' and not pii_allowed:
        out['messages']=[{**m,'text':redacted_text(m['text'])} for m in out['messages']]
        out['profile']={k:{**v,'evidence':redacted_text(v.get('evidence',''))} for k,v in out['profile'].items()}
    out['task']=serialize(task) if task else None
    if p['kind']=='visitor' and task:
        out['task']={'id':task.id,'status':task.status,'mode':task.mode}
    lead=session.scalar(rows(session,Lead,p['tenant']).where(Lead.conversation_id==c.id))
    out['lead']={'name':lead.name,'consent':lead.withdrawn_at is None} if lead else None
    if lead and p['kind']=='employee':
        out['lead']['phone']='已保护'
        if pii_allowed and lead.withdrawn_at is None:
            out['lead']['phone']=protect(lead.phone_cipher,decrypt=True) if lead.phone_cipher else ''
    if p['kind']=='employee':
        if task:
            owner=session.get(Member,(p['tenant'],task.owner_id)) if task.owner_id else None
            out['task'].update(task_presentation(task,owner,database_time(session)))
        out['handoff_summary']=handoff_summary(out['messages'],out['profile'])
        diagnostics=session.scalars(rows(session,Usage,p['tenant']).where(Usage.detail['conversation_id'].as_string()==c.id).order_by(Usage.created_at.desc()).limit(20)).all()
        out['answer_diagnostics']=[{'id':u.id,'kind':u.kind,'outcome':u.outcome,'latency_ms':u.latency_ms,'detail':u.detail}
                                   for u in diagnostics]
    keys=set(c.profile.get('need',{}).get('value','').split('、'))
    valid={k.id for k in session.scalars(rows(session,Knowledge,p['tenant']).where(Knowledge.status=='published',Knowledge.visibility=='public')).all()
           if not k.expires_at or k.expires_at>time.time()}
    if c.channel_id and channel and channel.config.get('knowledge_ids'):valid.intersection_update(channel.config['knowledge_ids'])
    out['recommendations']=[serialize(o) for o in session.scalars(rows(session,Offer,p['tenant']).where(Offer.active==True)).all()
        if o.knowledge_id in valid and keys.intersection(o.tags) and (o.price is None or c.profile.get('budget',{}).get('value',float('inf'))>=o.price)]
    return out

def handoff(session,p,c,lead=None):
    tid=p['tenant'];config=company(session,tid)
    channel=session.get(Channel,(tid,c.channel_id)) if c.channel_id else None
    skill=(channel.config.get('required_skill') or config.required_skill) if channel else config.required_skill
    now=database_time(session)
    task=get_task(session,tid,c.id)
    if task and task.status=='cancelled': raise HTTPException(409,'此联系已取消，请新建咨询。')
    if lead:
        if not lead['consent']: raise HTTPException(422,'请确认本次需求联系授权。')
        previous=session.scalar(rows(session,Lead,tid).where(Lead.conversation_id==c.id))
        if previous:
            if previous.withdrawn_at: raise HTTPException(409,'授权已撤回，请新建咨询。')
            return task
        scheduled=lead.get('schedule_at')
        qualified=candidates(session,tid,skill,now,live=False)
        if not qualified: raise HTTPException(409,'当前没有已配置的专业顾问，暂不能接受联系承诺。')
        if scheduled is not None:
            if not any(abs(x['at']-scheduled)<1 for x in appointment_slots(config,now)):
                raise HTTPException(422,'请选择有效的预约时间。')
            if scheduled<=now: raise HTTPException(422,'预约时间已过期。')
            mode='scheduled';start_at=scheduled
        else:
            if not in_hours(config,now) or not candidates(session,tid,skill,now):
                raise HTTPException(409,'当前无法提供即时专业联系，请选择预约时间。')
            mode='immediate';start_at=now
        session.add(Lead(tenant_id=tid,conversation_id=c.id,name=lead['name'],
            phone_cipher=protect(lead['phone']) if lead.get('phone') else '',consent_at=now,consent_text=CONSENT))
        c.name=lead['name']
        if task:
            task.mode=mode;task.accepted_at=now;task.start_at=start_at;task.deadline_at=start_at+300;task.sla='scheduled' if scheduled else 'running'
        else:
            task=Task(tenant_id=tid,conversation_id=c.id,skill=skill,accepted_at=now,start_at=start_at,
                      deadline_at=start_at+300,mode=mode,sla='scheduled' if scheduled else 'running')
            session.add(task);session.flush()
        task.policy={'timezone':config.timezone,'weekdays':config.weekdays,'opens':config.opens,'closes':config.closes,'consent_text':CONSENT}
    elif not task:
        task=Task(tenant_id=tid,conversation_id=c.id,skill=skill,accepted_at=now)
        session.add(task);session.flush()
    c.state='HANDOFF_PENDING' if task.status!='claimed' else 'HUMAN_ACTIVE'
    session.add(Outbox(tenant_id=tid,key='new:'+task.id,kind='new_task',task_id=task.id,version=task.version,
                      payload={'text':'有新的客户接待需求，请进入工作台查看。'})) if not session.scalar(rows(session,Outbox,tid).where(Outbox.key=='new:'+task.id)) else None
    audit(session,tid,p['id'],'lead.accepted' if lead else 'handoff.requested',task.id)
    return task

def withdraw(session,p,c):
    now=database_time(session);tid=p['tenant']
    lead=session.scalar(rows(session,Lead,tid).where(Lead.conversation_id==c.id))
    if lead: lead.withdrawn_at=now;lead.phone_cipher=''
    task=get_task(session,tid,c.id)
    if task:
        task.cancelled_at=now;task.status='cancelled';task.version+=1
        if task.deadline_at:
            task.sla=('met' if task.attempted_at<=task.deadline_at else 'breached') if task.attempted_at else ('cancelled' if now<=task.deadline_at else 'breached')
    c.state='CLOSED'
    if c.channel_id and c.channel!='web':
        thread=session.scalar(rows(session,ExternalThread,tid).where(ExternalThread.conversation_id==c.id))
        if thread:thread.route_cipher='';thread.eligible_until=0
        for delivery in session.scalars(rows(session,ChannelDelivery,tid).where(ChannelDelivery.conversation_id==c.id,ChannelDelivery.status.in_(['pending','retry']))):delivery.status='cancelled';delivery.last_error='stale'
    audit(session,tid,p['id'],'consent.withdrawn',c.id)

def risk(content):
    return 'high' if re.search(r'价格|报价|\d+\s*元|退款|保证|资质|证书|承诺|工期|交付周期|优惠|赔偿',content) else 'low'

def redacted_text(text):
    text=re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)','[手机号已隐藏]',text)
    return re.sub(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}','[邮箱已隐藏]',text)
