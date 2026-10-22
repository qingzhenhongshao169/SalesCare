import json
from pathlib import Path
import pytest
from sqlalchemy import select
from server.turn_plan import build_turn_plan, direction_state, coverage_issue
from server import model_gateway
from mvp.db import transaction, rows
from mvp.models import Knowledge, Usage
from mvp.knowledge_retrieval import PostgresScan
from test_case_recommendations import cases
from test_conversation import send, enable

ROOT=Path(__file__).resolve().parents[2]
MATRIX=json.loads((ROOT/'mvp/evaluation/monday-feedback-p1-v2-20261020.json').read_text('utf-8'))['cases']
COMPOUND=MATRIX[0]['text']


@pytest.mark.parametrize('item',MATRIX)
def test_frozen_feedback_tasks(item):
    plan=build_turn_plan([{'role':'user','content':item['text']}])
    assert plan['tasks']==item['tasks']
    assert plan['direction']['status']==item['direction']
    if 'budget' in item:assert plan['budget_status']==item['budget']


def test_undecided_is_user_state_not_assistant_suggestion():
    history=[{'role':'user','content':'具体做网站还是系统还没定'},
             {'role':'assistant','content':'建议先做官网。'},
             {'role':'user','content':'你先给个建议。'}]
    plan=build_turn_plan(history)
    assert plan['direction']['candidates']==['企业官网','管理系统']
    assert plan['direction']['status']=='undecided'
    assert coverage_issue('可以先从官网入手，后续再考虑系统。',plan)=='premature_direction'
    history.append({'role':'user','content':'那先做官网吧，业务系统以后再考虑'})
    assert direction_state(history)['status']=='selected'
    assert direction_state(history)['candidates']==['企业官网']


def keyword_only(monkeypatch,queries):
    # Reproduce the reported failure: ordinary retrieval returns only materials,
    # so case evidence must arrive through the approved case candidate path.
    def search(tid,channel,query,**kwargs):
        queries.append(query)
        with transaction(tid) as s:records=[r for r,_,_ in PostgresScan(s,tid,channel).scan(ids=['faq'])]
        return records,{'backend':'synthetic-keyword-only'}
    monkeypatch.setattr('mvp.worker.search_knowledge',search)


def test_one_generation_receives_case_and_materials_and_returns_same_card(make_tenant,make_employee,make_visitor,monkeypatch):
    t=make_tenant();a,_=make_employee(t);v,cid=make_visitor(t);cases(t,a,['export']);enable(t)
    queries=[];calls=[];keyword_only(monkeypatch,queries)
    def complete(cfg,messages):
        calls.append(messages)
        assert '新天激光品牌营销型外贸网站' in messages[0]['content']
        assert cfg['_turn_plan']['tasks']==['materials','cases']
        return json.dumps({'answer':'先准备公司介绍、产品资料和联系方式。相近方向可参考新天激光品牌营销型外贸网站：https://www.biaofun.com/case/5/26.html。',
            'knowledge_ids':['faq','export'],'covered_tasks':['materials','cases'],'case_ids':['export']},ensure_ascii=False)
    monkeypatch.setattr(model_gateway,'complete',complete)
    reply=send(t,v,cid,COMPOUND)
    detail=v.get('/api/public/conversations/'+cid).json()
    assert len(calls)==1 and '预算' not in queries[0]
    assert reply['case_recommendations'][0]['id']=='export'
    assert {x['id'] for x in reply['citations']}=={'faq','export'}
    assert detail['profile']['budget_status']['display']=='未定'
    assert detail['profile']['need']['status']=='selected'
    with transaction(t.id) as s:assert s.scalar(rows(s,Usage,t.id)).outcome=='llm'


@pytest.mark.parametrize('failure',['provider','omission','false_no_case','false_coverage','withdraw'])
def test_recovery_keeps_both_tasks_and_current_permissions(make_tenant,make_employee,make_visitor,monkeypatch,failure):
    t=make_tenant();a,_=make_employee(t);v,cid=make_visitor(t);cases(t,a,['export']);enable(t)
    keyword_only(monkeypatch,[])
    calls=[]
    def complete(cfg,messages):
        calls.append(1)
        if failure=='provider':raise model_gateway.ProviderError('synthetic provider failure')
        if failure=='withdraw':
            with transaction(t.id) as s:s.get(Knowledge,(t.id,'export')).status='withdrawn'
        return json.dumps({'answer':'先准备公司介绍、产品资料和联系方式。'+('没有可核实的案例。' if failure=='false_no_case' else ''),
            'knowledge_ids':['faq'],'covered_tasks':['materials','cases'] if failure=='false_coverage' else ['materials'],'case_ids':[]},ensure_ascii=False)
    monkeypatch.setattr(model_gateway,'complete',complete)
    reply=send(t,v,cid,COMPOUND)
    assert len(calls)==1
    assert '公司介绍' in reply['text'] and '产品' in reply['text']
    if failure=='withdraw':
        assert not reply['case_recommendations'] and not any(k['id']=='export' for k in reply['citations'])
        assert '没有找到可核实' in reply['text']
    else:
        assert '新天激光品牌营销型外贸网站' in reply['text']
        assert reply['case_recommendations'][0]['id']=='export'


def test_profile_reply_and_recommendations_share_undecided_direction(make_tenant,make_visitor):
    t=make_tenant();v,cid=make_visitor(t)
    for i,text in enumerate(['我们想做数字化升级','具体做网站还是系统还没定','你先给个建议。']):
        reply=send(t,v,cid,text,str(i))
    detail=v.get('/api/public/conversations/'+cid).json()
    need=detail['profile']['need']
    assert need['status']=='undecided' and not need['confirmed'] and '未定' in need['display']
    assert '官网' in reply['text'] and '内部' in reply['text'] and reply['text'].count('？')<=1
    assert not detail['recommendations'] and not reply['case_recommendations']
    send(t,v,cid,'那就先做官网吧','choose')
    assert v.get('/api/public/conversations/'+cid).json()['profile']['need']['status']=='selected'


def test_website_case_does_not_become_dispatch_system_experience(make_tenant,make_employee,make_visitor):
    t=make_tenant();a,_=make_employee(t);v,cid=make_visitor(t);cases(t,a,['logistics'])
    reply=send(t,v,cid,'你们给物流公司做过运输调度系统吗？我不要展示型官网')
    assert not reply['case_recommendations'] and '没有找到可核实' in reply['text']
    assert '佳怡' not in reply['text']


def test_verified_url_digits_do_not_authorize_an_unverified_price(monkeypatch):
    history=[{'role':'user','content':'给个官网案例'}]
    record={'id':'case','title':'工业官网','content':'工业官网建设经验','version':1,'source_url':'https://www.biaofun.com/case/5/26.html'}
    plan=build_turn_plan(history);plan['case_evidence']=[{'id':'case','title':'工业官网','summary':'工业官网建设经验','url':record['source_url']}]
    cfg={**model_gateway.DEFAULT,'_turn_plan':plan}
    def output(answer):return json.dumps({'answer':answer,'knowledge_ids':['case']},ensure_ascii=False)
    monkeypatch.setattr(model_gateway,'complete',lambda *_:output('可参考工业官网：https://www.biaofun.com/case/5/26.html。'))
    assert model_gateway.grounded_answer(cfg,'企业',history,[record])[0]
    monkeypatch.setattr(model_gateway,'complete',lambda *_:output('报价99999元。参考https://www.biaofun.com/case/5/26.html。'))
    with pytest.raises(model_gateway.ProviderError,match='未核实数字'):
        model_gateway.grounded_answer(cfg,'企业',history,[record])
    record={**record,'source_url':'报价99999元'}
    with pytest.raises(model_gateway.ProviderError,match='未核实数字'):
        model_gateway.grounded_answer(cfg,'企业',history,[record])


def test_general_comparison_needs_no_decorative_company_citation(monkeypatch):
    history=[{'role':'user','content':'网站还是系统没定，给个建议'}]
    plan=build_turn_plan(history)
    records=[{'id':'service','title':'企业服务','content':'提供网站服务','version':1}]
    monkeypatch.setattr(model_gateway,'complete',lambda *_:json.dumps({'answer':'官网偏向对外展示，业务系统偏向内部流程。您先想改善哪一环呢？',
        'knowledge_ids':[],'covered_tasks':['compare_directions'],'case_ids':[]},ensure_ascii=False))
    assert model_gateway.grounded_answer({**model_gateway.DEFAULT,'_turn_plan':plan},'企业',history,records)[1]==[]
    monkeypatch.setattr(model_gateway,'complete',lambda *_:json.dumps({'answer':'我们提供官网与业务系统开发，您想选哪一个呢？',
        'knowledge_ids':[],'covered_tasks':['compare_directions'],'case_ids':[]},ensure_ascii=False))
    with pytest.raises(model_gateway.ProviderError):
        model_gateway.grounded_answer({**model_gateway.DEFAULT,'_turn_plan':plan},'企业',history,records)


def test_choosing_bare_system_updates_response_and_profile_together(make_tenant,make_visitor):
    from server.dialogue_policy import dialogue_context
    history=[{'role':'user','content':'网站还是系统没定'},{'role':'user','content':'那就先做系统吧'}]
    assert dialogue_context(history)['topics']==['管理系统']
    assert build_turn_plan(history)['tasks']==['confirm_direction']
    t=make_tenant();v,cid=make_visitor(t)
    send(t,v,cid,history[0]['content'],'undecided')
    reply=send(t,v,cid,history[1]['content'],'selected')
    assert '管理系统' in reply['text'] and '官网' not in reply['text']
    assert v.get('/api/public/conversations/'+cid).json()['profile']['need']['candidates']==['管理系统']


def test_exact_case_faq_shares_case_evidence_without_generation(make_tenant,make_employee,make_visitor,monkeypatch):
    t=make_tenant();a,_=make_employee(t);v,cid=make_visitor(t);cases(t,a,['export']);enable(t)
    question='有工业外贸官网案例吗？';answer='官网案例可参考已发布项目资料。'
    with transaction(t.id) as s:s.get(Knowledge,(t.id,'faq')).content=answer
    assert a.put('/api/quick-replies/faq',json={'version':1,'questions':[question],'answer':answer,'confirmed':True}).status_code==200
    monkeypatch.setattr(model_gateway,'grounded_answer',lambda *args:pytest.fail('Reviewed FAQ must not call model'))
    reply=send(t,v,cid,question)
    assert reply['case_recommendations'][0]['id']=='export'
    assert '新天激光品牌营销型外贸网站' in reply['text']
    with transaction(t.id) as s:
        usage=s.scalar(rows(s,Usage,t.id))
        assert usage.kind=='faq' and usage.outcome=='faq' and usage.tokens==0
        assert usage.detail['flow']['generation_calls']==0
