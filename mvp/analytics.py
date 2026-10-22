"""Auditable cohort analytics. No inference model or chat hot-path work."""
from collections import Counter,defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
import re,time
from sqlalchemy import select,exists,case,func
from .models import Conversation,Message,Usage,Audit,ChannelDelivery,AnswerFeedback,AnalyticsRecord,Channel,CompanySettings
from .db import rows
from .answering import social_intent,operational_reply
from .service import redacted_text

VERSION='customer-analytics-v1'
MAX_COHORT=2000
TOPICS=[('预算与报价',r'预算|报价|费用|价格|多少钱|收费','商业条件'),('交付与排期',r'上线|交付|排期|工期|多久','商业条件'),('系统对接',r'对接|接口|集成|API','开发服务'),('官网建设',r'官网|网站|建站','开发服务'),('小程序',r'小程序|微商城','开发服务'),('管理系统',r'CRM|ERP|管理系统|业务系统','开发服务'),('APP与软件',r'APP|软件|定制开发','开发服务'),('营销推广',r'GEO|SEO|SEM|推广|营销|运营','营销服务'),('售后与使用',r'售后|保修|维修|故障|使用','产品服务')]

def themes(text):
    return [{'name':label,'group':group} for label,pattern,group in TOPICS if re.search(pattern,text,re.I)] or [{'name':'其他咨询','group':'其他'}]

def business_question(text):
    return bool(text and len(text.strip())>=2 and text!='[用户已删除内容]' and not social_intent(text) and not operational_reply(text))

def projection(c,record,messages,usage,feedback,deliveries,known_test=False):
    nature=record.data_kind if record else 'test' if known_test else 'unknown'
    visitors=[m for m in messages if m.role=='visitor' and business_question(m.text)]
    deleted=bool(messages and all(m.text=='[用户已删除内容]' for m in messages))
    delivered={d.message_id for d in deliveries if d.status=='accepted'}
    def sent(m):return c.channel=='web' or m.id in delivered
    def is_business_reply(m):
        previous=next((v for v in reversed(messages) if v.role=='visitor' and v.sequence<m.sequence),None)
        return bool(previous and business_question(previous.text))
    replies=[m for m in messages if m.role=='ai' and sent(m) and is_business_reply(m)]
    humans=[m for m in messages if m.role=='human' and sent(m)]
    latest_sequence=max((m.sequence for m in visitors),default=0)
    resolution=record.resolution if record and record.reviewed_sequence>=latest_sequence else 'unknown'
    if deleted:resolution='unknown'
    resolved=bool(resolution=='resolved' and record.confirmed_all and replies and not humans and not deleted)
    if deleted:feedback=[]
    issues=[];seen=set();by_id={m.id:m for m in messages};review=record.reviews if record else {}
    def issue(message,reason,topic_text=None):
        key=message.id+':'+reason
        if any(i['key']==key for i in issues):return
        issues.append({'key':key,'message_id':message.id,'sequence':message.sequence,'reason':reason,
                       'text':redacted_text(message.text)[:240],'topics':themes(topic_text or message.text),
                       'status':review.get(key,{}).get('status','pending')})
    for m in visitors:
        normalized=re.sub(r'[\s，,。！？!?]','',m.text).lower()
        if normalized in seen:issue(m,'重复说明')
        seen.add(normalized)
        if re.search(r'不是这个意思|答非所问|理解错|我说的是|不是.{0,12}是|更正|改成|预算变|预算改',m.text):issue(m,'更正或需求变化')
    for u in usage:
        m=by_id.get(u.detail.get('message_id'))
        if not m or m not in visitors:continue
        if u.outcome.startswith('fallback_'):
            issue(m,'回答回退')
            if not u.detail.get('retrieved') and u.outcome not in ('fallback_auth','fallback_quota','fallback_connection','fallback_timeout','fallback_circuit_open'):issue(m,'资料不足线索')
    for f in feedback:
        if f.value=='help' and f.id in by_id:
            prior=next((v for v in reversed(visitors) if v.sequence<by_id[f.id].sequence),None)
            if prior:issue(by_id[f.id],'客户仍需帮助',prior.text)
    topic_map={x['name']:x for m in visitors for x in themes(m.text)}
    topic_messages=defaultdict(list)
    for m in visitors:
        for t in themes(m.text):topic_messages[t['name']].append(m.id)
    return {'id':c.id,'created_at':c.created_at,'channel_id':c.channel_id,'channel':c.channel,'data_kind':nature,
            'ai_served':bool(replies),'human_contact':bool(humans),'resolved':resolved,'resolution':resolution,
            'closed':c.state=='CLOSED','deleted':deleted,'sequence':c.sequence,
            'latest_question':redacted_text(visitors[-1].text)[:180] if visitors else '',
            'topics':list(topic_map.values()),'issues':issues,'feedback_count':len(feedback),
            'feedback_help':sum(f.value=='help' for f in feedback),'feedback_solved':sum(f.value=='solved' for f in feedback),
            'evidence_message_id':record.evidence_message_id if record else '', '_topic_messages':dict(topic_messages)}

def chart_topics(groups):
    def output(g):return {'name':g['name'],'group':g['group'],'conversations':len(g['ids']),'issue_count':len(g['issues']),'reasons':dict(g['reasons'])}
    topics=sorted([output(g) for g in groups.values()],key=lambda t:(-t['conversations'],t['name']))
    if len(topics)>12:
        names=[t['name'] for t in topics[11:]];ids=set();keys=set();reasons=Counter()
        for n in names:
            ids.update(groups[n]['ids']);keys.update(groups[n]['issues']);reasons.update(groups[n]['reasons'])
        reasons=Counter(k.split(':',1)[1] for k in keys if ':' in k)
        topics=topics[:11]+[{'name':'其他主题','group':'其他','topic_names':names,'conversations':len(ids),'issue_count':len(keys),'reasons':dict(reasons)}]
    return topics

def known_tests(s,tid):
    return set(s.scalars(select(Audit.target).where(Audit.tenant_id==tid,Audit.action.like('verification.%'),Audit.detail['synthetic'].as_boolean()==True)))

def project_many(s,tid,conversations):
    ids=[c.id for c in conversations]
    if not ids:return []
    grouped={}
    for model in (Message,Usage,AnswerFeedback,ChannelDelivery):
        key=Usage.detail['conversation_id'].as_string() if model is Usage else model.conversation_id
        q=rows(s,model,tid).where(key.in_(ids))
        if model is Message:q=q.order_by(Message.sequence)
        result=defaultdict(list)
        for item in s.scalars(q):result[item.detail['conversation_id'] if model is Usage else item.conversation_id].append(item)
        grouped[model]=result
    meta={r.id:r for r in s.scalars(rows(s,AnalyticsRecord,tid).where(AnalyticsRecord.id.in_(ids)))}
    tests=known_tests(s,tid)
    return [projection(c,meta.get(c.id),*[grouped[m][c.id] for m in (Message,Usage,AnswerFeedback,ChannelDelivery)],c.id in tests) for c in conversations]

def snapshot(s,tid,start=0,end=0,channel='all',data_kind='business'):
    end=end or time.time()+1
    test=exists(select(Audit.id).where(Audit.tenant_id==tid,Audit.target==Conversation.id,Audit.action.like('verification.%'),Audit.detail['synthetic'].as_boolean()==True))
    nature=case((AnalyticsRecord.id.is_not(None),AnalyticsRecord.data_kind),(test,'test'),else_='unknown')
    base=select(Conversation).outerjoin(AnalyticsRecord,(AnalyticsRecord.tenant_id==Conversation.tenant_id)&(AnalyticsRecord.id==Conversation.id)).where(Conversation.tenant_id==tid,Conversation.created_at>=start,Conversation.created_at<end)
    if channel!='all':base=base.where(Conversation.channel=='web') if channel=='web' else base.where(Conversation.channel_id==channel)
    counts={k:0 for k in ('business','test','unknown')}
    for k,n in s.execute(select(nature,func.count()).select_from(Conversation).outerjoin(AnalyticsRecord,(AnalyticsRecord.tenant_id==Conversation.tenant_id)&(AnalyticsRecord.id==Conversation.id)).where(*base.whereclause.clauses).group_by(nature)):counts[k]=n
    if data_kind!='all':base=base.where(nature==data_kind)
    cs=list(s.scalars(base.order_by(Conversation.created_at.desc(),Conversation.id).limit(MAX_COHORT+1)))
    truncated=len(cs)>MAX_COHORT;items=project_many(s,tid,cs[:MAX_COHORT])
    timezone=(s.get(CompanySettings,(tid,'primary')).timezone or 'Asia/Shanghai')
    trend=defaultdict(lambda:{'consultations':0,'ai_served':0,'resolved':0,'human':0})
    groups={};questions={};all_issues=[]
    for item in items:
        for t in item['topics']:
            g=questions.setdefault(t['name'],{'name':t['name'],'group':t['group'],'ids':set(),'issues':set(),'reasons':Counter()})
            g['ids'].add(item['id']);g['issues'].update(item['_topic_messages'][t['name']])
        item.pop('_topic_messages',None)
        day=datetime.fromtimestamp(item['created_at'],ZoneInfo(timezone)).strftime('%Y-%m-%d')
        for name,field in [('consultations',None),('ai_served','ai_served'),('resolved','resolved'),('human','human_contact')]:trend[day][name]+=int(item[field]) if field else 1
        for i in item['issues']:
            all_issues.append({**i,'conversation_id':item['id'],'data_kind':item['data_kind']})
            if i['status']!='pending':continue
            for t in i['topics']:
                g=groups.setdefault(t['name'],{'name':t['name'],'group':t['group'],'ids':set(),'issues':set(),'reasons':Counter()})
                g['ids'].add(item['id']);g['issues'].add(i['key']);g['reasons'][i['reason']]+=1
    closed=[i for i in items if i['closed'] and i['ai_served'] and not i['deleted']]
    outcomes=Counter('resolved' if i['resolved'] else 'human' if i['human_contact'] else 'unresolved' if i['resolution']=='unresolved' else 'unknown' for i in closed)
    return {'version':VERSION,'as_of':time.time(),'timezone':timezone,'scope':{'start':start,'end':end,'channel':channel,'data_kind':data_kind,'cohort':'conversation_created_at','limit':MAX_COHORT,'truncated':truncated},'coverage':counts,
            'metrics':{'conversations':len(items),'ai_served':sum(i['ai_served'] for i in items),'resolved':sum(i['resolved'] for i in items),'human':sum(i['human_contact'] for i in items),
                       'closed_ai':len(closed),'closed_resolved':outcomes['resolved'],'closed_unknown':outcomes['unknown'],'closed_human':outcomes['human'],'closed_unresolved':outcomes['unresolved'],
                       'feedback_conversations':sum(i['feedback_count']>0 for i in items),'feedback_solved':sum(i['feedback_solved'] for i in items),'feedback_help':sum(i['feedback_help'] for i in items),
                       'pending_issues':sum(i['status']=='pending' for i in all_issues)},
            'trend':[{'day':day,**v} for day,v in sorted(trend.items())],'topics':chart_topics(groups),'question_topics':chart_topics(questions),'items':items,
            'channels':[{'id':c.id,'name':c.name} for c in s.scalars(rows(s,Channel,tid))]}
