import json
import time
from pathlib import Path
import pytest
from sqlalchemy import select, text
from mvp.models import Knowledge, CompanySettings, Conversation, Message, AIJob, uid
from mvp.db import transaction, rows
from mvp.case_recommendations import context, rank_cases, policy, select_for_message, recommendation_intent
from mvp.worker import lease_ai, run_ai, enforce_retention
from mvp.service import add_message
from test_conversation import send, enable

ROOT = Path(__file__).resolve().parents[2]
CATALOGUE = json.loads((ROOT/'knowledge/biaofun-case-studies-20261019.json').read_text('utf-8'))
MATRIX = json.loads((ROOT/'mvp/evaluation/case-recommendation-context-v3.json').read_text('utf-8'))['cases']
ALIASES = {'26':'export', '21':'industrial', '17':'energy', '27':'member', '24':'architecture', '14':'logistics'}


def cases(t, client, only=None):
    for entry in CATALOGUE['entries']:
        alias = ALIASES[entry['id'].rsplit('-', 1)[-1]]
        if only and alias not in only:
            continue
        with transaction(t.id) as s:
            s.add(Knowledge(tenant_id=t.id, id=alias, family=alias, title=entry['title'], content=entry['content'],
                source_url=entry['source_url'], status='published', author_id='synthetic-editor', reviewer_id='synthetic-publisher',
                keywords=['官网', '案例'], category='项目案例'))
        response = client.put('/api/case-recommendations/' + alias, json={'version':1, 'confirmed':True,
            **entry['recommendation'], 'priority':60 if alias=='export' else 50})
        assert response.status_code == 200, response.text


@pytest.mark.parametrize('scenario', MATRIX, ids=lambda s: s['id'] + '-' + s['split'])
def test_frozen_case_matrix(make_tenant, make_employee, make_visitor, scenario):
    t = make_tenant(); a, _ = make_employee(t); v, cid = make_visitor(t)
    target = make_tenant() if scenario.get('cross_tenant') else t
    target_client = make_employee(target)[0] if target.id != t.id else a
    cases(target, target_client, scenario.get('only'))
    if 'policy' in scenario:
        assert a.put('/api/case-recommendations/settings', json={**policy({}), **scenario['policy']}).status_code == 200
    change = scenario.get('change')
    if change:
        with transaction(t.id) as s:
            k = s.get(Knowledge, (t.id, 'export'))
            if change == 'withdraw': k.status = 'withdrawn'
            elif change == 'expire': k.expires_at = time.time()-1
            elif change == 'content': k.content += '\n来源已修改'
            elif change == 'url': k.source_url = 'https://www.biaofun.com/case/5/21.html'
            elif change == 'url_external': k.source_url = 'https://example.com/unverified'
            elif change == 'disable': k.case_recommendation = {**k.case_recommendation, 'enabled':False}
            elif change == 'version': k.version = 2
            elif change == 'internal': k.visibility = 'internal'
    if scenario.get('human'):
        with transaction(t.id) as s: s.get(Conversation, (t.id, cid)).state = 'HUMAN_ACTIVE'
    result = None
    for i, value in enumerate(scenario['messages']):
        response = v.post('/api/public/conversations/' + cid + '/messages', json={'text':value, 'client_id':f'case-{i}'})
        assert response.status_code == 200
        job = lease_ai(t.id)
        if job: run_ai(t.id, job)
        result = v.get('/api/public/conversations/' + cid).json()['messages'][-1]
    assert [c['id'] for c in result.get('case_recommendations', [])] == scenario['expected']
    for card in result.get('case_recommendations', []):
        assert card['url'].startswith('https://www.biaofun.com/case/')
        assert '费用和交期' in card['notice'] and '相关' in card['reason']
        assert 'evidence' not in card and 'signature' not in card


def test_policy_and_case_approval_are_authorized_and_validated(make_tenant, make_employee, make_visitor):
    t=make_tenant(); a,_=make_employee(t); editor,_=make_employee(t,['editor']); v,_=make_visitor(t)
    assert editor.get('/api/case-recommendations/settings').status_code == 403
    assert v.put('/api/case-recommendations/faq',json={'version':1}).status_code in (401,403)
    assert a.put('/api/case-recommendations/settings',json={'weights':{'service':90}}).status_code == 422
    assert a.put('/api/case-recommendations/settings',json={'min_business_turns':0}).status_code == 422
    assert a.put('/api/case-recommendations/faq',json={'version':1,'confirmed':True,'summary':'捏造事实','service_types':['website']}).status_code == 422
    assert a.put('/api/case-recommendations/faq',json={'version':1,'service_types':['arbitrary-code']}).status_code == 422


def test_fourth_turn_policy_and_explicit_repeat(make_tenant,make_employee,make_visitor):
    t=make_tenant(); a,_=make_employee(t); v,cid=make_visitor(t); cases(t,a,['export'])
    assert a.put('/api/case-recommendations/settings',json={**policy({}),'min_business_turns':4}).status_code == 200
    for i,value in enumerate(['要做工业外贸官网','主要面向海外客户','希望展示设备']):
        assert not send(t,v,cid,value,f'turn-{i}')['case_recommendations']
    assert not send(t,v,cid,'素材我们可以提供','four')['case_recommendations']
    assert send(t,v,cid,'官网怎么展示设备，有什么建议？','solution')['case_recommendations']
    assert not send(t,v,cid,'还想了解维护流程','five')['case_recommendations']
    assert send(t,v,cid,'可以再看看刚才的官网案例吗','six')['case_recommendations']


@pytest.mark.parametrize('change',['withdraw','source','policy'])
def test_old_cards_rechecked_on_get_and_sse(make_tenant,make_employee,make_visitor,change):
    t=make_tenant(); a,_=make_employee(t); v,cid=make_visitor(t); cases(t,a,['export'])
    reply=send(t,v,cid,'想看工业外贸官网案例')
    assert reply['case_recommendations']
    with transaction(t.id) as s:
        if change=='withdraw':s.get(Knowledge,(t.id,'export')).status='withdrawn'
        elif change=='source':s.get(Knowledge,(t.id,'export')).content+='\n变更'
        else:s.get(CompanySettings,(t.id,'primary')).case_recommendation_policy={'enabled':False}
    current=v.get('/api/public/conversations/'+cid).json()['messages'][-1]
    assert not current['case_recommendations']
    # DB retains the original attachment for audit, while public presentation suppresses it.
    with transaction(t.id) as s:
        stored=s.get(Message,(t.id,reply['id']))
        assert stored.case_recommendations
        from mvp.case_recommendations import public_cards
        assert not public_cards(s,t.id,None,stored.case_recommendations)


def test_case_does_not_add_a_second_model_call(make_tenant,make_employee,make_visitor,monkeypatch):
    t=make_tenant(); a,_=make_employee(t); v,cid=make_visitor(t); cases(t,a,['export']); enable(t); calls=[]
    def answer(cfg,name,history,records,**kwargs):
        calls.append(1)
        return '可以结合企业介绍和产品资料梳理官网内容。',records[:1]
    monkeypatch.setattr('mvp.worker.model_gateway.grounded_answer',answer)
    reply=send(t,v,cid,'想看工业外贸官网案例')
    assert len(calls)==1 and reply['case_recommendations']


def test_late_task_and_human_takeover_never_attach_cases(make_tenant,make_employee,make_visitor):
    t=make_tenant(); a,_=make_employee(t); v,cid=make_visitor(t); cases(t,a,['export'])
    with transaction(t.id) as s:
        c=s.get(Conversation,(t.id,cid)); first=add_message(s,c,'visitor','想看工业外贸官网案例')
        add_message(s,c,'visitor','改做CRM系统，不看官网')
        assert select_for_message(s,c,first,s.get(CompanySettings,(t.id,'primary')))==[]
        c.state='HUMAN_ACTIVE'
        assert select_for_message(s,c,first,s.get(CompanySettings,(t.id,'primary')))==[]


def test_delete_and_retention_clear_case_evidence(make_tenant,make_employee,make_visitor):
    t=make_tenant(); a,_=make_employee(t); v,cid=make_visitor(t); cases(t,a,['export'])
    reply=send(t,v,cid,'想看工业外贸官网案例')
    assert reply['case_recommendations']
    assert v.post('/api/public/conversations/'+cid+'/delete').status_code==200
    with transaction(t.id) as s:assert s.get(Message,(t.id,reply['id'])).case_recommendations==[]
    v2,cid2=make_visitor(t); reply2=send(t,v2,cid2,'想看工业外贸官网案例')
    with transaction(t.id) as s:
        c=s.get(Conversation,(t.id,cid2));c.state='CLOSED';c.created_at=time.time()-100*86400
    enforce_retention(t.id)
    with transaction(t.id) as s:assert s.get(Message,(t.id,reply2['id'])).case_recommendations==[]


def test_same_service_new_subject_and_hypothesis_are_conservative():
    first=context([{'role':'visitor','text':v} for v in ['我们是物流企业','要做官网','希望展示品牌']])
    assert first['industries']==['logistics'] and len(first['evidence'])==3
    ctx=context([{'role':'visitor','text':v} for v in ['想做工业外贸官网','另一个官网项目，暂时没想好需求','预算还没定']])
    assert not ctx['industries'] and not ctx['goals']
    ctx=context([{'role':'visitor','text':v} for v in ['想做工业外贸官网','主要面向海外','如果只有五千呢']])
    assert ctx['blocked']


def test_candidate_priority_cannot_override_service_or_relevance():
    ctx=context([{'role':'visitor','text':v} for v in ['我们是物流企业做品牌官网','希望展示企业形象','官网展示品牌有什么建议？']])
    items=[{'id':'wrong','service_types':['miniapp'],'industries':['logistics'],'goals':['branding'],'priority':100},
           {'id':'right','service_types':['website'],'industries':['logistics'],'goals':['branding'],'priority':0}]
    assert [v['id'] for v in rank_cases(ctx,items,policy({}))]==['right']


def test_offtopic_messages_and_deferred_case_requests_do_not_force_promotion():
    ctx=context([{'role':'visitor','text':v} for v in ['我们要做工业外贸官网','主要面向海外客户','今天天气如何']])
    assert ctx['turns']==2 and ctx['blocked']
    ctx=context([{'role':'visitor','text':'先问官网费用，后面再看案例'}])
    assert not ctx['explicit']


def test_catalogue_only_imports_drafts_for_biaofun(make_tenant,make_employee):
    t=make_tenant();a,_=make_employee(t)
    assert a.post('/api/case-catalogue/import',json={'apply':True}).status_code==403
    # This is the disposable test database, not the pilot tenant.
    from mvp.models import Tenant
    with transaction() as s:s.get(Tenant,t.id).slug='biaofun'
    preview=a.post('/api/case-catalogue/import',json={}).json()
    assert len(preview['drafts'])==6 and not preview['applied']
    result=a.post('/api/case-catalogue/import',json={'apply':True}).json()
    assert len(result['drafts'])==6
    with transaction(t.id) as s:
        entries=s.scalars(rows(s,Knowledge,t.id).where(Knowledge.category=='项目案例')).all()
        assert len(entries)==6 and all(k.status=='draft' and not k.case_recommendation['enabled'] for k in entries)
    assert not a.post('/api/case-catalogue/import',json={'apply':True}).json()['drafts']


def test_additive_migration_defaults_exist_on_rls_tables(make_tenant):
    t=make_tenant()
    with transaction(t.id) as s:
        for table,column in [('knowledge','case_recommendation'),('messages','case_recommendations'),('company_settings','case_recommendation_policy')]:
            assert s.scalar(text("SELECT column_default FROM information_schema.columns WHERE table_name=:t AND column_name=:c"),{'t':table,'c':column}) is not None
            flags=s.execute(text('SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname=:t'),{'t':table}).one()
            assert all(flags)


@pytest.mark.parametrize('text',[
    '预算8w','预算80000，不是8','现在变9万了','是预算变9万',
    '工期改成三周','可以介绍维护流程吗','官网要准备什么资料？',
    '这个案例很漂亮','案例链接打不开','预算九万够不够做这个官网？',
    '为什么给我推荐这个案例？','这个案例与我的需求不相关',
])
def test_turn_count_never_substitutes_for_current_recommendation_intent(text):
    assert recommendation_intent(text) is None
    ctx=context([{'role':'visitor','text':v} for v in ['要做工业外贸官网','主要面向海外客户','希望展示产品',text]])
    assert ctx['blocked']


@pytest.mark.parametrize('text',[
    '官网展示设备有什么建议？','你们能做这样的官网吗？','你们有什么相关经验吗？',
    '可以给我看看工业外贸官网案例吗？','预算九万，也想看一下相关官网案例',
    '你们做过外贸官网吗？',
])
def test_relevant_questions_can_invite_case_evidence(text):
    assert recommendation_intent(text)


def test_old_budget_turn_card_is_hidden_without_rewriting_history(make_tenant,make_employee,make_visitor):
    t=make_tenant();a,_=make_employee(t);v,cid=make_visitor(t);cases(t,a,['export'])
    reply=send(t,v,cid,'想看工业外贸官网案例')
    with transaction(t.id) as s:
        # Represents a card created by the old third-turn policy on a budget
        # correction. Keep the attachment for audit but remove public display.
        m=s.get(Message,(t.id,reply['case_recommendations'][0]['for_message_id']))
        m.text='是预算变9万'
    current=v.get('/api/public/conversations/'+cid).json()['messages'][-1]
    assert not current['case_recommendations']
    with transaction(t.id) as s:assert s.get(Message,(t.id,reply['id'])).case_recommendations
