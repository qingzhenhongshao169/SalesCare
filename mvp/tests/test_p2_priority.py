"""Remaining audit regressions; synthetic tenants in the isolated QA database."""
import time
import pytest
from server.turn_plan import build_turn_plan,coverage_issue,task_fallback
from mvp.answering import support_fallback
from mvp.db import transaction,rows
from mvp.models import Task,KBEngineConfig,Message,ContactAttempt
from mvp.knowledge_retrieval import search

@pytest.mark.parametrize('question,answer,unrelated',[
 ('小程序还是APP还没定','小程序适合已有平台内使用，APP需考虑独立安装。','官网'),
 ('GEO还是数字营销还没定','GEO与数字营销需要结合目标渠道比较。','内部流程'),
 ('官网还是管理系统还没定','官网用于展示，管理系统用于内部流程。','APP'),
])
def test_comparison_uses_actual_candidates(question,answer,unrelated):
 plan=build_turn_plan([{'role':'user','content':question}])
 assert coverage_issue(answer,plan) is None
 fallback,_=task_fallback(plan,[])
 assert unrelated not in fallback
 assert coverage_issue(fallback,plan) is None
 if unrelated=='官网':assert coverage_issue('官网侧重展示，管理系统处理内部流程。',plan)=='missing_comparison'

@pytest.mark.parametrize('wrapped',[False,True])
def test_second_table_chunk_is_used(wrapped):
 def row(a,b):return ('| ' if wrapped else '')+a+' | '+b+(' |' if wrapped else '')
 records=[{'id':'weight','title':'ABC · 仅限老客户','content':'ABC · 仅限老客户\n'+row('字段','参数')+'\n'+row('重量','10kg'),'document_id':'doc'},
          {'id':'warranty','title':'ABC · 仅限老客户','content':'ABC · 仅限老客户\n'+row('字段','参数')+'\n'+row('保修期','三年'),'document_id':'doc'}]
 answer,used=support_fallback(records,'保修期多久？')
 assert '三年' in answer and '10kg' not in answer and used[0]['id']=='warranty'
 assert '仅限老客户' in answer

def test_multicolumn_table_retains_conditions_and_empty_cells():
 record={'id':'x','title':'设备说明','document_id':'d','content':'设备说明\n字段 | 参数 | 适用条件\n保修期 | 三年 | 仅限老客户\n重量 |  | 待核实'}
 answer,used=support_fallback([record],'保修期多久')
 assert all(x in answer for x in ['三年','仅限老客户']) and used==[record]
 answer,_=support_fallback([record],'重量多少')
 assert '三年' not in answer and '待核实' in answer

def test_empty_table_cell_is_not_replaced_with_unrelated_value():
 record={'id':'x','title':'设备','document_id':'d','content':'设备\n字段 | 参数\n重量 | 10kg\n保修期 | '}
 answer,used=support_fallback([record],'保修期多久')
 assert '保修期：未提供' in answer and '10kg' not in answer

def test_table_does_not_mix_documents():
 records=[{'id':'a','title':'A','document_id':'a','content':'字段 | 参数\n重量 | 10kg'},
          {'id':'b','title':'B','document_id':'b','content':'字段 | 参数\n保修期 | 三年'}]
 answer,used=support_fallback(records,'保修期多久')
 assert '三年' not in answer and used[0]['id']=='a'

@pytest.mark.parametrize('mode,remaining,expected',[('api',None,1),('off',None,0),('api',lambda:0.02,0)])
def test_keyword_rerank_is_independent(make_tenant,monkeypatch,mode,remaining,expected):
 t=make_tenant()
 with transaction(t.id) as s:s.add(KBEngineConfig(tenant_id=t.id,id='primary',config={'embedding_mode':'keyword','rerank_mode':mode}))
 calls=[]
 monkeypatch.setattr('mvp.knowledge_retrieval.rerank',lambda q,r,c,k:(calls.append(r) or list(reversed(r)),'api_model'))
 records,diag=search(t.id,None,'企业官网',remaining=remaining)
 assert records and len(calls)==expected
 assert diag['rerank_reason']==('executed' if expected else 'disabled' if mode=='off' else 'insufficient_time')

def test_pending_ignores_historical_range_and_other_tenants(make_tenant,make_employee,make_visitor):
 t=make_tenant();a,_=make_employee(t);v,cid=make_visitor(t)
 assert v.post('/api/public/conversations/'+cid+'/handoff',json={}).status_code==200
 with transaction(t.id) as s:
  task=s.scalar(rows(s,Task,t.id).where(Task.conversation_id==cid));task.accepted_at=time.time()-86400
 body=a.get('/api/dashboard',params={'start':time.time()-3600}).json()
 assert body['pending']==1 and body['conversations']==1 and body['leads']==0
 other=make_tenant();b,_=make_employee(other)
 assert b.get('/api/dashboard').json()['pending']==0

def test_new_appointment_has_new_cycle(make_tenant,make_employee,make_visitor,monkeypatch):
 t=make_tenant();a,_=make_employee(t);v,cid=make_visitor(t)
 task=v.post('/api/public/conversations/'+cid+'/handoff',json={}).json()
 with transaction(t.id) as s:task=s.scalar(rows(s,Task,t.id).where(Task.conversation_id==cid));tid=task.id;version=task.version
 claimed=a.post('/api/tasks/'+tid+'/actions/claim',json={'version':version})
 assert claimed.status_code==200
 with transaction(t.id) as s:version=s.get(Task,(t.id,tid)).version
 assert a.post('/api/tasks/'+tid+'/contact',json={'version':version,'text':'已了解您的需求','client_id':'old-human'}).status_code==200
 slots=v.get('/api/public/config').json()['schedule'];at=slots[0]['at']
 assert v.post('/api/public/conversations/'+cid+'/lead',json={'name':'测试预约','consent':True,'schedule_at':at}).status_code==200
 with transaction(t.id) as s:
  task=s.get(Task,(t.id,tid));assert task.attempted_at is None and task.reached_at is None and task.version>version
  assert s.scalar(rows(s,ContactAttempt,t.id).where(ContactAttempt.task_id==tid)) is not None
  assert s.scalar(rows(s,Message,t.id).where(Message.client_id=='old-human')) is not None
  version=task.version
 assert v.post('/api/public/conversations/'+cid+'/lead',json={'name':'测试预约','consent':True,'schedule_at':at}).status_code==200
 with transaction(t.id) as s:assert s.get(Task,(t.id,tid)).version==version
 assert a.post('/api/tasks/'+tid+'/contact',json={'version':version,'text':'提前联系','client_id':'before-start'}).status_code==409
 monkeypatch.setattr('mvp.worker.database_time',lambda s:at+301)
 from mvp.worker import tick_sla
 tick_sla(t.id)
 with transaction(t.id) as s:assert s.get(Task,(t.id,tid)).sla=='breached'

def test_dashboard_rejects_pre_cycle_attempt_as_fulfillment(make_tenant,make_employee,make_visitor):
 t=make_tenant();a,_=make_employee(t);v,cid=make_visitor(t)
 v.post('/api/public/conversations/'+cid+'/handoff',json={})
 now=time.time()
 with transaction(t.id) as s:
  task=s.scalar(rows(s,Task,t.id).where(Task.conversation_id==cid))
  task.mode='scheduled';task.accepted_at=now-1000;task.start_at=now-600;task.deadline_at=now-300;task.attempted_at=now-1200;task.sla='met'
 body=a.get('/api/dashboard').json()
 assert body['scheduled_due']==1 and body['scheduled_met']==0 and body['scheduled_rate']==0

def test_legacy_pre_cycle_contact_cannot_hide_overdue_task(make_tenant,make_employee,make_visitor):
 from mvp.reception import task_stage
 from mvp.worker import tick_sla
 t=make_tenant();make_employee(t);v,cid=make_visitor(t)
 v.post('/api/public/conversations/'+cid+'/handoff',json={})
 now=time.time()
 with transaction(t.id) as s:
  task=s.scalar(rows(s,Task,t.id).where(Task.conversation_id==cid));tid=task.id
  task.mode='scheduled';task.accepted_at=now-1000;task.start_at=now-600;task.deadline_at=now-300;task.attempted_at=now-1200;task.reached_at=now-1100;task.sla='met'
  assert task_stage(task,now)=='overdue'
 tick_sla(t.id)
 with transaction(t.id) as s:
  task=s.get(Task,(t.id,tid))
  assert task.sla=='breached' and task.attempted_at is None and task.reached_at is None
