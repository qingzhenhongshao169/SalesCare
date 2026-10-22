"""Latest customer budget must agree in profile, fallback and model validation."""
import json
import pytest
from server.requirements import budget_update, timeline_update, update_profile_requirements
from server.dialogue_policy import dialogue_context, quality_issue
from server.answer_policy import numbers_supported
from mvp.answering import contextual_fallback
from mvp.db import transaction
from mvp.models import CompanySettings
from test_conversation import send, enable


@pytest.mark.parametrize('text',[
    '预算可能只有5000元了，可以先做简单一点，后面再增加功能吗？',
    '预算改为5k','预算现在只有5K','预算大概是5千元','预算是五千元',
    '预算从1万元降到5000元','预算改成5,000元','预算目前暂定为5000元',
])
def test_colloquial_budget_updates(text):
    assert budget_update(text)['value']==5000


@pytest.mark.parametrize('text',[
    '不是预算5000元','预算不是5000元','如果预算5000元，能做吗？',
    '假如预算只有5000元会怎么样？','预算5000到10000元','预算五千到一万元','预算500页',
    '项目需要5000人','他给的报价5000元','合同编号5000',
])
def test_unstated_or_hypothetical_budget_is_not_recorded(text):
    assert budget_update(text) is None


@pytest.mark.parametrize('text',['预算先不设','预算目前不确定','预算取消'])
def test_explicit_budget_clear(text):
    assert budget_update(text)=={'action':'clear'}


def test_timeline_is_a_customer_wish():
    assert timeline_update('希望两周上线')['value']=='两周'
    assert timeline_update('上线时间要尽快2周')['value']=='2周'
    assert timeline_update('两周内上线')['value']=='两周'
    assert timeline_update('不是希望两周上线') is None


def test_budget_units_are_equivalent_for_grounding():
    for text in ['预算5k','预算5K','预算5千元','预算五千元']:
        assert numbers_supported('您提到的预算是5000元。','',[{'role':'user','content':text}])
        assert not numbers_supported('报价5000元就能做。','',[{'role':'user','content':text}])


def test_latest_budget_survives_pressure_for_a_guarantee():
    history=[{'role':'user','content':'我想做企业官网展示公司，预算1万元，希望两周上线。'},
             {'role':'assistant','content':'请整理资料。'},
             {'role':'user','content':'预算可能只有5000元了，可以先做简单一点，后面再增加功能吗？'}]
    context=dialogue_context(history)
    assert context['scene']=='budget_update' and context['budget_changed']
    assert budget_update(context['user_requirements']['budget'])['value']==5000
    reply,_=contextual_fallback([],history)
    assert '5000' in reply and '1万' not in reply and '后续扩展' in reply
    assert quality_issue('明白，您提到预算1万元。',history)=='stale_budget'
    history.append({'role':'user','content':'那你直接确认5000元、两周一定能做完吧。'})
    context=dialogue_context(history)
    assert context['scene']=='deadline'
    assert quality_issue('您的预算1万元，可以整理资料。',history)=='stale_budget'
    reply,_=contextual_fallback([],history)
    assert '5000' in reply and '1万' not in reply and '是否能做到' in reply


@pytest.mark.parametrize('mode',['sales','general_support'])
def test_public_profile_and_recovery_answer_both_use_latest_budget(make_tenant,make_visitor,mode):
    t=make_tenant();v,cid=make_visitor(t)
    if mode=='general_support':
        with transaction(t.id) as s:s.get(CompanySettings,(t.id,'primary')).service_mode=mode
    send(t,v,cid,'我想做企业官网，预算1万元，希望两周上线。','initial')
    latest='预算可能只有5000元了，可以先做简单一点，后面再增加功能吗？'
    answer=send(t,v,cid,latest,'correction')
    detail=v.get('/api/public/conversations/'+cid).json()
    assert detail['profile']['budget']['value']==5000
    assert detail['profile']['budget']['evidence']==latest
    assert detail['profile']['timeline']['value']=='两周'
    if mode=='sales':assert '5000' in answer['text'] and '1万' not in answer['text']
    send(t,v,cid,'预算先不设，上线时间也不确定。','clear')
    profile=v.get('/api/public/conversations/'+cid).json()['profile']
    assert 'budget' not in profile and 'timeline' not in profile


def test_model_stale_budget_is_rejected_on_later_followup(make_tenant,make_visitor,monkeypatch):
    t=make_tenant();v,cid=make_visitor(t)
    send(t,v,cid,'我要企业官网，预算1万元，希望两周上线。','initial')
    send(t,v,cid,'预算可能只有5000元了。','correction')
    enable(t)
    monkeypatch.setattr('mvp.worker.model_gateway.complete',lambda *_:json.dumps({
        'answer':'明白，您提到预算1万元，可以先整理公司介绍。','knowledge_ids':[]},ensure_ascii=False))
    answer=send(t,v,cid,'那你直接确认5000元、两周一定能做完吧。','pressure')
    assert '5000' in answer['text'] and '1万' not in answer['text'] and '是否能做到' in answer['text']


@pytest.mark.parametrize('text,value',[
    ('预算8w',80000),('预算8W',80000),('预算8.5w',85000),('预算九万',90000),
    ('预算变9万',90000),('是预算变9万',90000),('预算变为九万元',90000),
    ('预算从8w改成9w',90000),('预算80000，不是8',80000),
])
def test_monetary_aliases_and_explicit_change_verbs(text,value):
    assert budget_update(text)['value']==value


@pytest.mark.parametrize('text',[
    '预算8watt','预算8w到9w','预算八万到九万','预算是9万吗？',
    '现在变9万了','现在变9人了','现在变9周了','报价变9万了',
])
def test_ambiguous_or_unestablished_amount_does_not_become_budget(text):
    assert budget_update(text) is None


def test_contextual_change_is_shared_by_profile_answer_and_grounding():
    history=[];profile={}
    for text,expected in [('预算8w',80000),('预算80000，不是8',80000),('现在变9万了',90000),('是预算变9万',90000)]:
        history.append({'role':'user','content':text})
        profile=update_profile_requirements(profile,text)
        assert profile['budget']['value']==expected and profile['budget']['evidence']==text
        facts=dialogue_context(history)
        assert budget_update(facts['user_requirements']['budget'])['value']==expected
        assert numbers_supported(f'您的预算是{expected}元。','',history)
    assert quality_issue('您的预算80000元。',history)=='stale_budget'
    assert not numbers_supported('报价90000元就能做。','',history)
    assert not numbers_supported('您的预算80000元。','',[{'role':'user','content':'设备功率8W。'}])
    assert budget_update('改成9人',previous_budget=80000) is None
    assert budget_update('另一个项目变9万',previous_budget=80000) is None
    assert budget_update('如果变9万呢',previous_budget=80000) is None


def test_reported_budget_sequence_updates_sidebar_and_rejects_stale_model(make_tenant,make_visitor,monkeypatch):
    t=make_tenant();v,cid=make_visitor(t)
    send(t,v,cid,'想做企业官网展示产品','start')
    enable(t)
    # Simulate the exact stale answer; the local guard must recover using the
    # same current monetary state the sidebar uses, even if generation fails.
    monkeypatch.setattr('mvp.worker.model_gateway.complete',lambda *_:json.dumps({
        'answer':'好的，您的官网预算80000元，我会按最新需求继续梳理。','knowledge_ids':['faq']},ensure_ascii=False))
    for i,(text,expected) in enumerate([('预算8w',80000),('预算80000，不是8',80000),('现在变9万了',90000),('是预算变9万',90000)]):
        answer=send(t,v,cid,text,f'budget-{i}')
        detail=v.get('/api/public/conversations/'+cid).json()
        assert detail['profile']['budget']['value']==expected
        assert not answer['case_recommendations']
        if expected==90000:
            assert quality_issue(answer['text'],[{'role':'user','content':'想做企业官网，预算90000元'}])!='stale_budget'
            assert budget_update(answer['text'])['value']==90000
