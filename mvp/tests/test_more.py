import time
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from mvp.app import app
from mvp.db import transaction,rows
from mvp.models import *
from mvp.worker import lease_ai,run_ai,enforce_retention
from mvp.service import mark_profile

def test_login_returns_identity_for_immediate_composer(make_tenant,make_employee):
    t=make_tenant();client,m=make_employee(t)
    response=client.post('/api/login',json={'tenant':t.slug,'username':m.username,'password':'synthetic-test-password-only'})
    assert response.json()['id']==m.id
    assert 'httponly' in response.headers['set-cookie'].lower() and 'secure' in response.headers['set-cookie'].lower()

def test_origin_and_oversized_body_blocked(make_tenant,make_employee):
    t=make_tenant();client,m=make_employee(t)
    assert client.post('/api/presence',json={'available':True},headers={'Origin':'https://evil.example'}).status_code==403
    assert client.post('/api/login',content='x'*260000).status_code==413

def test_inactive_tenant_revokes_visitors(make_tenant,make_visitor):
    t=make_tenant();v,cid=make_visitor(t)
    with transaction() as session:session.get(Tenant,t.id).active=False
    assert v.get('/api/public/conversations/'+cid).status_code==401

def test_expired_ai_lease_can_be_recovered(make_tenant,make_visitor):
    t=make_tenant();v,cid=make_visitor(t)
    v.post('/api/public/conversations/'+cid+'/messages',json={'text':'企业官网建设需要准备什么？','client_id':'recover'})
    first=lease_ai(t.id)
    with transaction(t.id) as session:session.get(AIJob,(t.id,first)).lease_until=time.time()-1
    second=lease_ai(t.id);assert first==second
    run_ai(t.id,second)
    with transaction(t.id) as session:assert session.get(AIJob,(t.id,first)).status=='done'

def test_knowledge_revoked_during_model_run_not_delivered(make_tenant,make_visitor,monkeypatch):
    from server.model_gateway import protect
    t=make_tenant();v,cid=make_visitor(t)
    with transaction(t.id) as session:
        row=session.get(ModelConfig,(t.id,'primary'));row.config={'enabled':True,'model':'synthetic'};row.cipher=protect('synthetic-key')
    def generated(cfg,name,history,records):
        with transaction(t.id) as session:session.get(Knowledge,(t.id,'faq')).status='withdrawn'
        return '过期的模型回答',[records[0]]
    monkeypatch.setattr('mvp.worker.model_gateway.grounded_answer',generated)
    v.post('/api/public/conversations/'+cid+'/messages',json={'text':'企业官网建设需要准备什么？','client_id':'revoke-source'})
    run_ai(t.id,lease_ai(t.id))
    messages=v.get('/api/public/conversations/'+cid).json()['messages']
    assert messages[-1]['citations']==[] and messages[-1]['text']!='过期的模型回答'

def test_budget_correction_and_negated_people_not_recorded(make_tenant,make_visitor):
    t=make_tenant();v,cid=make_visitor(t)
    with transaction(t.id) as session:
        c=session.get(Conversation,(t.id,cid));mark_profile(session,c,'不是35人，是50人；预算3万元，预算改为5万元。计划下周启动。')
        assert c.profile['people']['value']==50 and c.profile['budget']['value']==50000 and c.profile['timeline']['value']=='下周'

def test_retention_anonymizes_only_closed_and_keeps_sla(make_tenant,make_visitor):
    t=make_tenant();v,cid=make_visitor(t)
    with transaction(t.id) as session:
        c=session.get(Conversation,(t.id,cid));c.created_at=time.time()-100*86400;c.state='CLOSED'
        session.add(Task(tenant_id=t.id,conversation_id=cid,skill='专业咨询',accepted_at=c.created_at,start_at=c.created_at,deadline_at=c.created_at+300,sla='breached'))
    enforce_retention(t.id)
    with transaction(t.id) as session:
        assert session.get(Conversation,(t.id,cid)).name=='已匿名化访客'
        task=session.scalar(rows(session,Task,t.id).where(Task.conversation_id==cid));assert task.sla=='breached' and task.deadline_at-task.accepted_at==300

def test_callback_receipt_not_reached(make_tenant,make_employee,make_visitor):
    t=make_tenant();employee,m=make_employee(t);v,cid=make_visitor(t)
    v.post('/api/public/conversations/'+cid+'/handoff',json={})
    with transaction(t.id) as session:task=session.scalar(rows(session,Task,t.id).where(Task.conversation_id==cid))
    claim=employee.post('/api/tasks/'+task.id+'/actions/claim',json={'version':task.version}).json()
    reply=employee.post('/api/tasks/'+task.id+'/contact',json={'version':claim['version'],'text':'您好，专业顾问已接待。','client_id':'ack'}).json()
    assert v.post('/api/public/conversations/'+cid+'/received/'+str(reply['sequence']),json={}).status_code==200
    with transaction(t.id) as session:
        assert session.get(Message,(t.id,reply['id'])).received_at
        assert session.get(Task,(t.id,task.id)).reached_at is None

def test_capacity_blocks_second_claim(make_tenant,make_employee,make_visitor):
    t=make_tenant();employee,m=make_employee(t);v,cid=make_visitor(t);other,second=make_visitor(t)
    with transaction(t.id) as session:session.get(Member,(t.id,m.id)).capacity=1
    for client,identity in ((v,cid),(other,second)):client.post('/api/public/conversations/'+identity+'/handoff',json={})
    with transaction(t.id) as session:tasks=list(session.scalars(rows(session,Task,t.id).order_by(Task.created_at)))
    assert employee.post('/api/tasks/'+tasks[0].id+'/actions/claim',json={'version':tasks[0].version}).status_code==200
    assert employee.post('/api/tasks/'+tasks[1].id+'/actions/claim',json={'version':tasks[1].version}).status_code==409

def test_unassigned_staff_message_phone_is_redacted(make_tenant,make_employee,make_visitor):
    t=make_tenant();admin,m=make_employee(t,['admin']);v,cid=make_visitor(t)
    v.post('/api/public/conversations/'+cid+'/messages',json={'text':'预算3万元，我的号码13800000000，邮箱qa@example.test','client_id':'pii'})
    data=admin.get('/api/conversations/'+cid).json()
    assert '13800000000' not in data['messages'][-1]['text']
    assert '13800000000' not in data['profile']['budget']['evidence']
    assert 'qa@example.test' not in data['messages'][-1]['text']

def test_source_versions_preserve_then_supersede_published(make_tenant,make_employee,monkeypatch):
    from mvp.worker import sync_source
    t=make_tenant();a,m=make_employee(t)
    source=a.post('/api/knowledge-sources',json={'title':'官网更新来源','url':'https://www.biaofun.com/service'}).json()
    monkeypatch.setattr('mvp.worker.fetch_public',lambda *_:('官网服务资料第一版本，内容发生变化也必须经过审核。','hash-one'))
    sync_source(t.id,source['id'])
    with transaction(t.id) as session:first=session.scalar(rows(session,Knowledge,t.id).where(Knowledge.family==source['id']))
    assert a.post('/api/knowledge/'+first.id+'/publish',json={'version':1}).status_code==200
    monkeypatch.setattr('mvp.worker.fetch_public',lambda *_:('官网服务资料第二版本，需要企业业务负责人再次审核。','hash-two'))
    sync_source(t.id,source['id'])
    with transaction(t.id) as session:
        second=session.scalar(rows(session,Knowledge,t.id).where(Knowledge.family==source['id'],Knowledge.version==2))
        assert second.status=='draft' and session.get(Knowledge,(t.id,first.id)).status=='published'
    assert a.post('/api/knowledge/'+second.id+'/publish',json={'version':2}).status_code==200
    with transaction(t.id) as session:assert session.get(Knowledge,(t.id,first.id)).status=='superseded'

def test_model_destination_change_drops_key_and_test_requirement(make_tenant,make_employee,monkeypatch):
    t=make_tenant();admin,m=make_employee(t,['admin'])
    body={'protocol':'openai','base_url':'https://api.deepseek.com','model':'deepseek-flash','api_key':'synthetic-only','enabled':False}
    response=admin.put('/api/models',json=body);assert response.status_code==200,response.text
    assert response.json()['has_key'] and response.json()['api_key'] is None
    body['api_key']='';body['enabled']=True
    assert admin.put('/api/models',json=body).status_code==409
    monkeypatch.setattr('mvp.app.model_gateway.complete',lambda *_:'连接正常')
    assert admin.post('/api/models/test',json={}).status_code==200
    assert admin.put('/api/models',json=body).status_code==200
    body.update(base_url='https://dashscope.aliyuncs.com/compatible-mode/v1',model='qwen-plus',enabled=False)
    changed=admin.put('/api/models',json=body);assert changed.status_code==200 and not changed.json()['has_key']

def test_automated_high_risk_source_requires_two_human_reviews(make_tenant,make_employee):
    t=make_tenant();a,_=make_employee(t,['admin']);b,_=make_employee(t,['publisher'])
    with transaction(t.id) as session:
        candidate=Knowledge(tenant_id=t.id,family=uid(),title='价格来源更新',content='价格来源发生变化，需要两位企业人员复核。',risk='high',author_id='source-sync')
        session.add(candidate);session.flush()
    first=a.post('/api/knowledge/'+candidate.id+'/publish',json={'version':1})
    assert first.status_code==200 and first.json()['status']=='draft'
    assert a.post('/api/knowledge/'+candidate.id+'/publish',json={'version':1}).status_code==409
    second=b.post('/api/knowledge/'+candidate.id+'/publish',json={'version':1})
    assert second.status_code==200 and second.json()['status']=='published'
    assert len(second.json()['approvals'])==2
    assert b.post('/api/knowledge/'+candidate.id+'/withdraw',json={'version':1}).status_code==200
    restored=b.post('/api/knowledge/'+candidate.id+'/publish',json={'version':1})
    assert restored.status_code==200 and restored.json()['status']=='published'
    assert len(restored.json()['approvals'])==2

def test_empty_embed_allowlist_has_valid_http_header(make_tenant,make_employee):
    t=make_tenant();a,_=make_employee(t)
    with transaction(t.id) as session:
        session.get(CompanySettings,(t.id,'primary')).allowed_origins=[]
    response=a.get('/chat/'+t.slug)
    csp=response.headers['content-security-policy']
    assert csp.endswith("frame-ancestors 'self'")
    assert csp==csp.strip() and '\n' not in csp and '\r' not in csp

def test_local_admin_recovery_revokes_old_session(make_tenant,make_employee):
    from mvp.manage import recover_admin
    from mvp.security import verify_password
    t=make_tenant();client,m=make_employee(t,['admin'])
    with transaction(t.id) as session:
        recover_admin(session,t.id,m.username,'new-synthetic-recovery-password')
    assert client.get('/api/me').status_code==401
    with transaction(t.id) as session:
        member=session.get(Member,(t.id,m.id))
        assert verify_password(member.password_hash,'new-synthetic-recovery-password')

def test_claim_reports_actionable_skill_and_presence_errors(make_tenant,make_employee,make_visitor):
    t=make_tenant();agent,m=make_employee(t);v,cid=make_visitor(t)
    v.post('/api/public/conversations/'+cid+'/handoff',json={})
    with transaction(t.id) as session:
        task=session.scalar(rows(session,Task,t.id).where(Task.conversation_id==cid))
        session.get(Member,(t.id,m.id)).skills=['顾问 技术']
    endpoint='/api/tasks/'+task.id+'/actions/claim'
    denied=agent.post(endpoint,json={'version':task.version})
    assert denied.status_code==409 and task.skill in denied.json()['detail'] and '技能不匹配' in denied.json()['detail']
    with transaction(t.id) as session:
        member=session.get(Member,(t.id,m.id));member.skills=[task.skill];member.available=False
    offline=agent.post(endpoint,json={'version':task.version})
    assert offline.status_code==409 and '离线' in offline.json()['detail']
    with transaction(t.id) as session:
        member=session.get(Member,(t.id,m.id));member.available=True;member.heartbeat_at=time.time()-100
    stale=agent.post(endpoint,json={'version':task.version})
    assert stale.status_code==409 and '在线连接已过期' in stale.json()['detail']
    assert agent.post('/api/presence',json={'available':True}).status_code==200
    assert agent.post(endpoint,json={'version':task.version}).status_code==200

def test_staff_reload_restores_only_fresh_online_preference(make_tenant,make_employee):
    t=make_tenant();agent,m=make_employee(t,['agent'])
    assert agent.get('/api/me').json()['available'] is True
    with transaction(t.id) as session:session.get(Member,(t.id,m.id)).heartbeat_at=time.time()-100
    assert agent.get('/api/me').json()['available'] is False
    with transaction(t.id) as session:assert session.get(Member,(t.id,m.id)).available is True
