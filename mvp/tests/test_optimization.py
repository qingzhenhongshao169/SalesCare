import json
from pathlib import Path
import time
import pytest
from sqlalchemy import select
from mvp.answering import resolve_question,grounded_fallback
from mvp.db import transaction,rows
from mvp.models import AIJob,Message,Usage,Task,Member,CompanySettings,Outbox,Notice,Conversation
from mvp.security import serialize
from mvp.service import add_message
from mvp.worker import lease_ai,run_ai,deliver_notice,tick_sla
from server.model_gateway import protect
from test_conversation import enable,send

CASES=json.loads((Path(__file__).parents[1]/'evaluation/multi-turn-cases.json').read_text('utf-8'))
@pytest.mark.parametrize('case',CASES,ids=[x['id'] for x in CASES])
def test_multi_turn_user_topic_resolution(case):
    history=[{'role':'user','content':x} for x in [*case['prior'],case['question']]]
    query,clarification,route=resolve_question(case['question'],history)
    assert route==case['route']
    assert bool(clarification)==(route=='clarify_topic')
    if case['anchor']:assert case['anchor'] in query

def test_model_claims_do_not_establish_user_topic():
    history=[{'role':'user','content':'你好'},{'role':'assistant','content':'您要做CRM'},{'role':'user','content':'具体呢？'}]
    _,clarification,_=resolve_question('具体呢？',history)
    assert clarification and 'CRM' not in clarification

def test_source_fallback_retains_qualifications_and_one_citation():
    records=[{'id':'one','content':'提供咨询。可先沟通范围。具体工期需要顾问确认。'},{'id':'two','content':'无关长资料'}]
    answer,used=grounded_fallback(records)
    assert '工期需要顾问确认' in answer and '无关' not in answer and len(used)==1

def test_one_conversation_jobs_are_ordered_and_history_is_anchored(make_tenant,make_visitor,monkeypatch):
    t=make_tenant();v,cid=make_visitor(t);enable(t)
    assert v.post('/api/public/conversations/'+cid+'/messages',json={'text':'企业官网建设需要准备什么？','client_id':'first'}).status_code==200
    # Public ingress currently rejects another in-flight send (409). Keep that rule;
    # simulate a recovered/backlogged durable job directly in the isolated test DB.
    with transaction(t.id) as s:
        c=s.get(Conversation,(t.id,cid));m=add_message(s,c,'visitor','我们改做CRM',client_id='second')
        s.add(AIJob(tenant_id=t.id,conversation_id=cid,message_id=m.id))
    first=lease_ai(t.id)
    assert first and lease_ai(t.id) is None
    captured=[]
    def answer(cfg,company,history,records):
        captured.append(history)
        return '可以先提供企业介绍和参考网站。',records[:1]
    monkeypatch.setattr('mvp.worker.model_gateway.grounded_answer',answer)
    run_ai(t.id,first)
    assert captured and all('改做CRM' not in m['content'] for m in captured[0])
    second=lease_ai(t.id);assert second and second!=first
    run_ai(t.id,second)
    with transaction(t.id) as s:
        us=list(s.scalars(rows(s,Usage,t.id)));assert all(u.detail.get('job_id') and u.detail.get('message_id') for u in us)

def test_ambiguous_topic_clarifies_without_gap_or_model(make_tenant,make_visitor,monkeypatch):
    t=make_tenant();v,cid=make_visitor(t);send(t,v,cid,'我们需要官网和CRM','topics');enable(t)
    monkeypatch.setattr('mvp.worker.model_gateway.complete',lambda *_:pytest.fail('Ambiguous topic must be clarified first'))
    reply=send(t,v,cid,'具体多少钱？','ambiguous')
    assert '哪一项' in reply['text'] and not reply['citations']

def handoff(t,v,cid):
    response=v.post('/api/public/conversations/'+cid+'/handoff',json={});assert response.status_code==200
    # Visitor responses intentionally hide version, owner and internal deadlines.
    with transaction(t.id) as s:
        return serialize(s.scalar(rows(s,Task,t.id).where(Task.conversation_id==cid)))

def test_reception_stages_summary_and_source_privacy(make_tenant,make_visitor,make_employee):
    t=make_tenant();v,cid=make_visitor(t);client,member=make_employee(t)
    v.post('/api/public/conversations/'+cid+'/messages',json={'text':'我的电话13800138000，想做网站','client_id':'source'})
    task=handoff(t,v,cid)
    assert client.get('/api/conversations?stage=waiting').json()[0]['id']==cid
    detail=client.get('/api/conversations/'+cid).json()
    assert '13800138000' not in json.dumps(detail['handoff_summary'])
    before=task['deadline_at']
    assert client.post('/api/tasks/'+task['id']+'/actions/claim',json={'version':task['version']}).status_code==200
    assert client.get('/api/reception-summary').json()['counts']['first_reply']==1
    detail=client.get('/api/conversations/'+cid).json()
    assert detail['task']['needs_first_reply'] and detail['task']['deadline_at']==before
    assert detail['handoff_summary']['latest_question']['message_id']
    assert client.get('/api/conversations?stage=mine').json()[0]['id']==cid
    with transaction(t.id) as s:s.get(Member,(t.id,member.id)).heartbeat_at=time.time()-120
    assert client.get('/api/reception-summary').json()['owner_attention']==1
    assert client.get('/api/conversations/'+cid).json()['task']['owner_attention']

def test_appointment_and_overdue_are_derived_without_mutation(make_tenant,make_visitor,make_employee):
    t=make_tenant();v,cid=make_visitor(t);client,_=make_employee(t);task=handoff(t,v,cid)
    with transaction(t.id) as s:
        row=s.get(Task,(t.id,task['id']));row.mode='scheduled';row.start_at=time.time()+3600;row.deadline_at=row.start_at+300
        deadline=row.deadline_at
    assert client.get('/api/conversations?stage=appointment').json()[0]['id']==cid
    with transaction(t.id) as s:
        row=s.get(Task,(t.id,task['id']));assert row.deadline_at==deadline
        row.start_at=time.time()-400;row.deadline_at=time.time()-100
    assert client.get('/api/conversations?stage=overdue').json()[0]['id']==cid

def test_diagnostics_and_queues_are_scoped(make_tenant,make_visitor,make_employee):
    a=make_tenant();b=make_tenant();va,cid=make_visitor(a);ca,_=make_employee(a);cb,_=make_employee(b)
    send(a,va,cid,'我们想做官网')
    assert ca.get('/api/conversations/'+cid).json()['answer_diagnostics']
    assert cb.get('/api/conversations/'+cid).status_code==404
    assert cb.get('/api/reception-summary').json()['counts']['ai']==0
    assert 'answer_diagnostics' not in va.get('/api/public/conversations/'+cid).json()

def test_notification_failure_retry_and_permissions(make_tenant,make_visitor,make_employee,monkeypatch):
    t=make_tenant();v,cid=make_visitor(t);admin,_=make_employee(t);agent,_=make_employee(t,roles=['agent'])
    task=handoff(t,v,cid)
    with transaction(t.id) as s:
        cfg=s.get(CompanySettings,(t.id,'primary'));cfg.notification_kind='wecom';cfg.notification_cipher=protect('https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=synthetic')
    monkeypatch.setattr('mvp.worker.send_notification',lambda *_:(False,'rate_limited'))
    deliver_notice(t.id)
    events=admin.get('/api/notification-events').json()['items']
    assert events[0]['status']=='retry' and events[0]['last_error']=='rate_limited'
    assert 'synthetic' not in json.dumps(events)
    assert agent.get('/api/notification-events').status_code==403
    with transaction(t.id) as s:s.scalar(rows(s,Outbox,t.id)).due_at=time.time()-1
    monkeypatch.setattr('mvp.worker.send_notification',lambda *_:(True,''));deliver_notice(t.id)
    assert admin.get('/api/notification-events').json()['items'][0]['status']=='delivered'
    assert len(admin.get('/api/notices').json()['items'])==1
    with transaction(t.id) as s:assert s.get(Task,(t.id,task['id'])).attempted_at is None

def test_stale_reminders_suppressed_before_send(make_tenant,make_visitor,make_employee,monkeypatch):
    t=make_tenant();v,cid=make_visitor(t);admin,_=make_employee(t);task=handoff(t,v,cid)
    admin.post('/api/tasks/'+task['id']+'/actions/claim',json={'version':task['version']})
    monkeypatch.setattr('mvp.worker.send_notification',lambda *_:pytest.fail('Stale reminder was sent'))
    assert deliver_notice(t.id)
    assert admin.get('/api/notification-events').json()['items'][0]['status']=='cancelled'

def test_inflight_notice_does_not_retry_cancelled_task(make_tenant,make_visitor,make_employee,monkeypatch):
    t=make_tenant();v,cid=make_visitor(t);admin,_=make_employee(t);task=handoff(t,v,cid)
    with transaction(t.id) as s:
        cfg=s.get(CompanySettings,(t.id,'primary'));cfg.notification_kind='wecom';cfg.notification_cipher=protect('synthetic-protected-url')
    def received(*_):
        with transaction(t.id) as s:s.get(Task,(t.id,task['id'])).status='cancelled'
        return False,'timeout'
    monkeypatch.setattr('mvp.worker.send_notification',received);deliver_notice(t.id)
    event=admin.get('/api/notification-events').json()['items'][0]
    assert event['status']=='cancelled' and not event['current']

def test_notice_gives_up_after_six_attempts_without_recording_contact(make_tenant,make_visitor,make_employee,monkeypatch):
    t=make_tenant();v,cid=make_visitor(t);admin,_=make_employee(t);task=handoff(t,v,cid)
    with transaction(t.id) as s:
        cfg=s.get(CompanySettings,(t.id,'primary'));cfg.notification_kind='wecom';cfg.notification_cipher=protect('synthetic')
    monkeypatch.setattr('mvp.worker.send_notification',lambda *_:(False,'timeout'))
    for _ in range(6):
        with transaction(t.id) as s:s.scalar(rows(s,Outbox,t.id)).due_at=time.time()-1
        deliver_notice(t.id)
    event=admin.get('/api/notification-events').json()['items'][0]
    assert event['attempts']==6 and event['status']=='failed'
    with transaction(t.id) as s:assert s.get(Task,(t.id,task['id'])).attempted_at is None

@pytest.mark.parametrize('status,payload,expected',[(200,{'errcode':0},(True,'')),(200,{'errcode':40013,'message':'synthetic-secret'},(False,'provider_rejected')),(429,{},(False,'rate_limited')),(503,{},(False,'provider_http'))])
def test_notification_provider_adapter_uses_bounded_errors(monkeypatch,status,payload,expected):
    import httpx
    from mvp.notifications import send_notification
    original=httpx.Client;requests=[]
    def respond(request):requests.append(request);return httpx.Response(status,json=payload)
    monkeypatch.setattr('mvp.notifications.httpx.Client',lambda **kwargs:original(transport=httpx.MockTransport(respond),**kwargs))
    result=send_notification('wecom',protect('https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=synthetic-secret'),'请进入工作台处理。')
    assert result==expected and 'synthetic-secret' not in str(result)
    assert requests and 'synthetic-secret' not in requests[0].content.decode()

def test_web_ingress_still_rejects_second_inflight_message(make_tenant,make_visitor):
    t=make_tenant();v,cid=make_visitor(t)
    assert v.post('/api/public/conversations/'+cid+'/messages',json={'text':'你好','client_id':'one'}).status_code==200
    assert v.post('/api/public/conversations/'+cid+'/messages',json={'text':'新需求','client_id':'two'}).status_code==409

def test_operation_guidance_does_not_execute_handoff(make_tenant,make_visitor):
    t=make_tenant();v,cid=make_visitor(t)
    reply=send(t,v,cid,'请转人工')
    assert '点击' in reply['text'] and '已' not in reply['text']
    with transaction(t.id) as s:
        assert s.get(Conversation,(t.id,cid)).state=='AI_ACTIVE'
        assert not s.scalar(rows(s,Task,t.id))

def test_claimed_first_reply_reminder_has_correct_reason_and_original_deadline(make_tenant,make_visitor,make_employee):
    t=make_tenant();v,cid=make_visitor(t);admin,_=make_employee(t);task=handoff(t,v,cid)
    admin.post('/api/tasks/'+task['id']+'/actions/claim',json={'version':task['version']})
    with transaction(t.id) as s:
        row=s.get(Task,(t.id,task['id']));row.mode='immediate';row.start_at=time.time()-35;row.deadline_at=row.start_at+300;deadline=row.deadline_at
    tick_sla(t.id)
    with transaction(t.id) as s:
        event=s.scalar(rows(s,Outbox,t.id).where(Outbox.kind=='sla'))
        assert event and '尚未专业回复' in event.payload['text']
        assert s.get(Task,(t.id,task['id'])).deadline_at==deadline
