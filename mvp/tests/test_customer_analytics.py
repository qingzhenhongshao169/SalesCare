"""Source-backed analytics using isolated synthetic tenants, never live visitors."""
import time
import pytest
from sqlalchemy import select
from mvp.db import transaction,rows
from mvp.models import Conversation,Message,Usage,AnalyticsRecord,AnswerFeedback,Audit

def seed(t,cid,kind='business',human=False,closed=False):
    with transaction(t.id) as s:
        c=s.get(Conversation,(t.id,cid))
        records=[]
        for role,text in [('visitor','官网报价多少，预算8万'),('ai','需要先核实建设范围。'),('visitor','预算改成9万'),('ai','已按最新预算继续梳理。')]+([('human','我是顾问。')] if human else []):
            c.sequence+=1;m=Message(tenant_id=t.id,conversation_id=cid,sequence=c.sequence,role=role,text=text);s.add(m);s.flush();records.append(m)
        record=s.get(AnalyticsRecord,(t.id,cid))
        if record:record.data_kind=kind
        else:s.add(AnalyticsRecord(tenant_id=t.id,id=cid,data_kind=kind))
        if closed:c.state='CLOSED'
        s.add(Usage(tenant_id=t.id,kind='llm',outcome='fallback_numbers',detail={'conversation_id':cid,'message_id':records[0].id,'retrieved':[{'id':'faq'}]}))
        return records,c.sequence

def snap(a,**params):
    r=a.get('/api/analytics',params=params);assert r.status_code==200,r.text;return r.json()

def test_default_excludes_unknown_and_explicit_tests(make_tenant,make_employee,make_visitor):
    t=make_tenant();a,_=make_employee(t)
    for k in ['business','unknown','test']:
        _,cid=make_visitor(t);seed(t,cid,k)
    data=snap(a)
    assert data['coverage']=={'business':1,'test':1,'unknown':1}
    assert data['metrics']['ai_served']==1 and len(data['items'])==1
    assert snap(a,data_kind='unknown')['items'][0]['data_kind']=='unknown'
    assert snap(a,data_kind='all')['metrics']['ai_served']==3

def test_synthetic_audit_is_explicit_test_evidence(make_tenant,make_employee,make_visitor):
    t=make_tenant();a,_=make_employee(t);_,cid=make_visitor(t);seed(t,cid,'unknown')
    with transaction(t.id) as s:
        s.delete(s.get(AnalyticsRecord,(t.id,cid)))
        s.add(Audit(tenant_id=t.id,actor='test',action='verification.synthetic',target=cid,detail={'synthetic':True}))
    assert snap(a)['coverage']['test']==1
    assert snap(a,data_kind='test')['items'][0]['id']==cid

def test_feedback_is_question_level_not_conversation_resolution(make_tenant,make_employee,make_visitor):
    t=make_tenant();a,_=make_employee(t);v,cid=make_visitor(t);messages,_=seed(t,cid)
    endpoint='/api/public/conversations/'+cid+'/feedback/'+messages[1].id
    assert v.put(endpoint,json={'value':'solved'}).status_code==200
    assert v.put(endpoint,json={'value':'solved'}).status_code==200
    data=snap(a);assert data['metrics']['resolved']==0 and data['metrics']['feedback_solved']==1
    assert v.put(endpoint,json={'value':'help'}).status_code==200
    assert any(i['reason']=='客户仍需帮助' for i in snap(a)['items'][0]['issues'])
    assert v.put(endpoint,json={'value':'clear'}).status_code==200
    assert snap(a)['metrics']['feedback_conversations']==0

def test_manual_resolution_requires_scope_evidence_and_resets_on_new_question(make_tenant,make_employee,make_visitor):
    t=make_tenant();a,_=make_employee(t);_,cid=make_visitor(t);messages,seq=seed(t,cid,closed=True)
    endpoint='/api/analytics/conversations/'+cid
    body={'sequence':seq,'resolution':'resolved','evidence_message_id':messages[1].id}
    assert a.put(endpoint,json=body).status_code==422
    body['confirmed_all']=True
    assert a.put(endpoint,json=body).status_code==200
    d=snap(a);assert d['metrics']['resolved']==1 and d['metrics']['closed_resolved']==1 and d['metrics']['closed_ai']==1
    with transaction(t.id) as s:
        c=s.get(Conversation,(t.id,cid));c.sequence+=1;s.add(Message(tenant_id=t.id,conversation_id=cid,sequence=c.sequence,role='visitor',text='还需要系统对接'))
    assert snap(a)['metrics']['resolved']==0
    assert a.put(endpoint,json=body).status_code==409

def test_human_reply_prevents_independent_resolution(make_tenant,make_employee,make_visitor):
    t=make_tenant();a,_=make_employee(t);_,cid=make_visitor(t);messages,seq=seed(t,cid,human=True,closed=True)
    assert a.put('/api/analytics/conversations/'+cid,json={'sequence':seq,'resolution':'resolved','evidence_message_id':messages[1].id,'confirmed_all':True}).status_code==200
    d=snap(a);assert d['metrics']['human']==1 and d['metrics']['resolved']==0
    m=d['metrics'];assert m['closed_human']==1 and m['closed_unknown']==0
    assert sum(m[k] for k in ['closed_resolved','closed_human','closed_unresolved','closed_unknown'])==m['closed_ai']

def test_topic_counts_deduplicate_and_review_does_not_publish(make_tenant,make_employee,make_visitor):
    t=make_tenant();a,_=make_employee(t);_,cid=make_visitor(t);_,seq=seed(t,cid)
    d=snap(a);assert sum(t['conversations'] for t in d['topics'])>=1
    assert all(t['conversations']==1 for t in d['topics'])
    issue=d['items'][0]['issues'][0]
    assert a.put('/api/analytics/conversations/'+cid,json={'sequence':seq,'issue_key':issue['key'],'issue_status':'needs_knowledge'}).status_code==200
    new=snap(a);assert new['metrics']['pending_issues']==d['metrics']['pending_issues']-1
    assert next(i for i in new['items'][0]['issues'] if i['key']==issue['key'])['status']=='needs_knowledge'

def test_tenant_authority_csrf_and_visitor_ownership(make_tenant,make_employee,make_visitor):
    t=make_tenant();a,_=make_employee(t);v,cid=make_visitor(t);messages,seq=seed(t,cid)
    other=make_tenant();b,_=make_employee(other);_,other_cid=make_visitor(other)
    assert snap(b)['items']==[]
    assert b.put('/api/analytics/conversations/'+cid,json={'sequence':seq,'data_kind':'test'}).status_code==404
    v2,_=make_visitor(t)
    assert v2.put('/api/public/conversations/'+cid+'/feedback/'+messages[1].id,json={'value':'solved'}).status_code==404
    assert v.put('/api/public/conversations/'+cid+'/feedback/'+messages[0].id,json={'value':'solved'}).status_code==404
    editor,_=make_employee(t,roles=['editor'])
    assert editor.get('/api/analytics').status_code==403
    agent,_=make_employee(t,roles=['agent'])
    assert agent.put('/api/analytics/conversations/'+cid,json={'sequence':seq,'data_kind':'business'}).status_code==403
    csrf=a.headers.pop('X-CSRF-Token')
    assert a.put('/api/analytics/conversations/'+cid,json={'sequence':seq,'data_kind':'test'}).status_code==403
    a.headers['X-CSRF-Token']=csrf
    with transaction(other.id) as s:assert not list(s.scalars(select(AnalyticsRecord).where(AnalyticsRecord.id==cid)))

def test_deleted_content_does_not_reappear_as_topics_or_feedback(make_tenant,make_employee,make_visitor):
    t=make_tenant();a,_=make_employee(t);v,cid=make_visitor(t);messages,_=seed(t,cid)
    v.put('/api/public/conversations/'+cid+'/feedback/'+messages[1].id,json={'value':'help'})
    assert v.post('/api/public/conversations/'+cid+'/delete',json={}).status_code==200
    d=snap(a);assert not d['topics'] and not d['items'][0]['issues'] and d['metrics']['feedback_help']==0

def test_dates_channels_and_greetings(make_tenant,make_employee,make_visitor):
    t=make_tenant();a,_=make_employee(t);v,cid=make_visitor(t);seed(t,cid)
    assert snap(a,start=time.time()+100)['items']==[]
    assert snap(a,channel='nonexistent')['items']==[]
    assert len(snap(a,channel='web')['items'])==1
    _,cid2=make_visitor(t)
    with transaction(t.id) as s:
        c=s.get(Conversation,(t.id,cid2));s.get(AnalyticsRecord,(t.id,cid2)).data_kind='business';c.sequence+=1;s.add(Message(tenant_id=t.id,conversation_id=cid2,sequence=c.sequence,role='visitor',text='你好'));c.sequence+=1;s.add(Message(tenant_id=t.id,conversation_id=cid2,sequence=c.sequence,role='ai',text='您好！'))
    assert snap(a)['metrics']['ai_served']==1
    assert a.get('/api/analytics',params={'start':10,'end':1}).status_code==422

def test_explicit_unknown_overrides_synthetic_audit_and_qa_defaults_test(make_tenant,make_employee,make_visitor):
    t=make_tenant();a,_=make_employee(t);_,cid=make_visitor(t)
    assert snap(a,data_kind='test')['items'][0]['id']==cid
    with transaction(t.id) as s:
        c=s.get(Conversation,(t.id,cid));seq=c.sequence
        s.add(Audit(tenant_id=t.id,actor='qa',action='verification.analytics',target=cid,detail={'synthetic':True}))
    assert a.put('/api/analytics/conversations/'+cid,json={'sequence':seq,'data_kind':'unknown'}).status_code==200
    assert snap(a,data_kind='unknown')['items'][0]['id']==cid

def test_all_topics_persist_after_issues_reviewed(make_tenant,make_employee,make_visitor):
    t=make_tenant();a,_=make_employee(t);_,cid=make_visitor(t);_,seq=seed(t,cid)
    d=snap(a);names={x['name'] for x in d['question_topics']};assert '官网建设' in names
    for issue in d['items'][0]['issues']:
        assert a.put('/api/analytics/conversations/'+cid,json={'sequence':seq,'issue_key':issue['key'],'issue_status':'checked'}).status_code==200
    d=snap(a);assert not d['topics'] and {x['name'] for x in d['question_topics']}==names

def test_merged_topics_deduplicate_conversations_and_issues():
    from mvp.analytics import chart_topics
    from collections import Counter
    groups={str(i):{'name':str(i),'group':'其他','ids':{'one'},'issues':{'same:回答回退'},'reasons':Counter({'回答回退':1})} for i in range(15)}
    out=chart_topics(groups);assert len(out)==12
    other=out[-1];assert other['conversations']==1 and other['issue_count']==1 and other['reasons']['回答回退']==1

def test_cohort_limit_is_explicit_and_coverage_is_full(make_tenant,make_employee,make_visitor,monkeypatch):
    t=make_tenant();a,_=make_employee(t)
    for _ in range(3):_,cid=make_visitor(t);seed(t,cid)
    monkeypatch.setattr('mvp.analytics.MAX_COHORT',2)
    d=snap(a);assert d['scope']['truncated'] and d['scope']['limit']==2
    assert d['coverage']['business']==3 and d['metrics']['conversations']==2
