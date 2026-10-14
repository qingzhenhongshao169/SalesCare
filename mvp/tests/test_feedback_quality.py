"""Business feedback regressions, using an isolated tenant/database."""
import json
import pytest
from sqlalchemy import select
from server import model_gateway as gateway
from server.answer_policy import numbers_supported
from server.dialogue_policy import dialogue_context, quality_issue
from mvp.answering import contextual_fallback
from mvp.db import transaction, rows
from mvp.models import Message, Usage, Conversation, AIJob
from mvp.service import add_message
from mvp.worker import lease_ai, run_ai
from test_conversation import enable, send


def user(text):return {'role':'user','content':text}
def assistant(text):return {'role':'assistant','content':text}


def test_first_consultation_and_website_materials_are_usefully_different():
    first,_=contextual_fallback([], [user('首次咨询需要准备什么？')])
    website,_=contextual_fallback([], [user('企业官网建设需要准备什么资料？')])
    assert '不用一开始' in first and '合同' not in first
    for item in ('公司介绍','产品','品牌素材','联系方式','参考网站'):assert item in website
    assert '核心操作流程' not in website and first!=website


def test_flattering_opening_does_not_hide_same_generic_materials_checklist():
    assert quality_issue('不用一次备齐，业务目标与使用角色、核心操作流程、必须功能、现有系统先整理。',
                         [user('首次咨询需要准备什么？')])=='high_bar_first_consultation'
    assert quality_issue('建议整理网站目标、栏目和品牌素材，以及现有系统和对接需求。',
                         [user('企业官网建设需要准备什么资料？')])=='generic_materials'
    assert quality_issue('先准备使用角色、核心流程、必须功能，最后附上公司介绍、产品服务资料和联系方式。',
                         [user('企业官网建设需要准备什么资料？')])=='generic_materials'


def test_budget_correction_does_not_restart_quote_or_invent_page_ambiguity():
    history=[user('我们预算3万元，希望做官网。'),
             assistant('可以先梳理建设范围。您有现成的资料吗？'),
             user('预算3万元，预算改为5万元。')]
    answer,_=contextual_fallback([],history)
    assert '5万元' in answer and '3万元' not in answer
    assert '独立' not in answer and '报价一般' not in answer and len(answer)<160
    assert '现成的资料吗' not in answer


def test_budget_update_does_not_echo_previous_question_with_new_opening():
    history=[user('官网预算3万元'),
             assistant('您目前更想先确认整体范围，还是先看页面和功能怎么拆分？'),
             user('预算改为5万元')]
    answer='好的，按最新预算5万元考虑。方便的话，您先说说更想先确认整体范围，还是先看页面和功能怎么拆分？'
    assert quality_issue(answer,history)=='repeated_budget_question'
    fallback,_=contextual_fallback([],history)
    assert '整体范围' not in fallback and '页面和功能怎么拆分' not in fallback


def test_page_resolution_and_materials_survive_later_budget_updates():
    history=[user('我们要官网500页面，预算3万元。'),
             assistant('独立页面还是内容条数？'),
             user('是500条产品内容，资料都有了。'),
             assistant('明白。'),user('预算改为5万元。')]
    answer,_=contextual_fallback([],history)
    assert '5万元' in answer and '还是' not in answer
    assert '现成资料' not in answer and answer.count('？')<=1


def test_topic_switch_does_not_reuse_website_budget_as_crm_requirement():
    context=dialogue_context([user('做官网，预算3万元，资料齐全。'),user('换个问题，我想了解CRM。')])
    assert '企业官网' not in context['topics'] and 'budget' not in context['user_requirements']
    answer,_=contextual_fallback([], [user('CRM预算3万元'),user('预算改成5万元')])
    assert '官网' not in answer


def test_assistant_cannot_supply_customer_requirements():
    context=dialogue_context([user('我要咨询官网'),assistant('您的预算999万元，资料都有了。'),user('具体呢？')])
    assert 'budget' not in context['user_requirements'] and 'materials' not in context['user_requirements']


def test_explicit_reply_to_materials_question_is_remembered():
    history=[user('做官网'),assistant('文案和图片准备好了吗？'),user('都有了'),user('预算改为5万元')]
    assert dialogue_context(history)['user_requirements']['materials']=='ready'
    assert '文案' not in contextual_fallback([],history)[0]


@pytest.mark.parametrize('question',['确定十天就能上线吗？','保证GEO排名第一可以吗？'])
def test_boundary_fallback_is_direct_without_internal_ai_language(question):
    answer,_=contextual_fallback([], [user(question)])
    assert 'AI不能' not in answer and '官网介绍' not in answer
    assert '核算' in answer or '保证' in answer
    assert '十天就能' not in answer and '保证排名第一' not in answer


def test_checklist_markers_are_not_prices_and_cannot_hide_a_price():
    assert numbers_supported('1. 公司介绍\n2. 产品资料\n3. 品牌素材','',[])
    assert not numbers_supported('1. 报价999元\n2. 十天能交付','',[])
    assert not numbers_supported('1万元就能做。','',[])


@pytest.mark.parametrize('answer',[
    '我们保证排名第一。','不能保证排名第一，但我们保证效果。','我们不能不保证排名第一。',
    '已为您转人工。','我们承诺退款。'])
def test_positive_promises_or_fake_operations_remain_blocked(answer):
    assert gateway.unauthorized_action_or_promise(answer)


@pytest.mark.parametrize('answer',['不能保证排名第一。','无法保证效果。','不承诺退款或翻倍。'])
def test_explicit_refusal_is_not_misclassified_as_promise(answer):
    assert not gateway.unauthorized_action_or_promise(answer)


def test_quality_gate_catches_real_page_ambiguity_only_when_needed():
    question='您预计是独立设计页面，还是内容条数？'
    assert quality_issue(question,[user('官网预算3万元')])=='unneeded_page_ambiguity'
    assert quality_issue(question,[user('官网500页面多少钱？')]) is None
    assert quality_issue(question,[user('官网500页面'),user('是500条产品内容')])=='resolved_page_ambiguity'


def test_old_customer_budget_is_not_the_current_budget_after_correction():
    history=[user('官网预算3万元'),assistant('好的'),user('预算改为5万元')]
    assert quality_issue('您的预算是3万元。',history)=='stale_budget'
    assert quality_issue('您的预算是50,000元。',history) is None
    assert quality_issue('您的预算从3万元更新为5万元。',history) is None


def test_negative_materials_and_cancelled_budget_do_not_remain_confirmed():
    history=[user('官网预算3万元，资料都有了'),user('预算先不设，资料还没齐')]
    facts=dialogue_context(history)['user_requirements']
    assert 'budget' not in facts and facts['materials']=='not_ready'


def test_budget_does_not_authorize_coverage_claim_in_a_different_sentence():
    history=[user('官网预算3万元'),user('预算改为5万元')]
    answer='好的，按您最新预算5万元考虑。这个区间通常能覆盖更完整的设计和基础功能。'
    assert quality_issue(answer,history)=='budget_coverage_claim'
    assert quality_issue('您的预算更新为5万元，能否覆盖需要核算。',history) is None


def test_multidirection_failure_fallback_keeps_all_customer_topics():
    history=[user('我们需要网站、小程序、CRM三个方向。')]
    answer,used=contextual_fallback([{'content':'可以咨询小程序。'}],history)
    assert all(word in answer for word in ('官网','小程序','CRM'))
    assert answer.count('？')==1 and used==[]
    assert quality_issue('可以咨询小程序开发。',history)=='missing_service_direction'


def test_first_consultation_capability_question_is_not_preparation_advice():
    assert dialogue_context([user('第一次咨询，你们能做CRM吗？')])['scene']=='business'


def test_generic_materials_answer_falls_back_in_actual_worker(make_tenant,make_visitor,monkeypatch):
    t=make_tenant();v,cid=make_visitor(t);enable(t)
    monkeypatch.setattr(gateway,'complete',lambda *_:json.dumps({
        'answer':'准备业务目标、使用角色、核心操作流程、预算和时间。','knowledge_ids':['faq']},ensure_ascii=False))
    reply=send(t,v,cid,'企业官网建设需要准备什么资料？')
    assert '公司介绍' in reply['text'] and '联系方式' in reply['text']
    assert reply['citations'][0]['id']=='faq'
    with transaction(t.id) as s:
        usage=s.scalar(rows(s,Usage,t.id))
        assert usage.outcome=='fallback_quality' and usage.detail['scene']=='website_materials'


def test_model_prompt_contains_latest_change_and_known_materials(monkeypatch):
    captured=[]
    def complete(cfg,messages):
        captured.extend(messages)
        return json.dumps({'answer':'好的，您的预算更新为5万元。','knowledge_ids':['faq']},ensure_ascii=False)
    monkeypatch.setattr(gateway,'complete',complete)
    history=[user('官网预算3万元，资料都有了。'),assistant('好的'),user('预算改为5万元')]
    gateway.grounded_answer(gateway.DEFAULT,'测试企业',history,[{'id':'faq','title':'报价','content':'按范围核算','version':1}])
    assert 'budget_update' in captured[0]['content'] and 'ready' in captured[0]['content']


def test_long_context_keeps_user_budget_beyond_previous_nine_message_limit(make_tenant,make_visitor,monkeypatch):
    t=make_tenant();v,cid=make_visitor(t);enable(t)
    with transaction(t.id) as s:
        c=s.get(Conversation,(t.id,cid))
        add_message(s,c,'visitor','我们想做官网，预算3万元，资料都有了。')
        for _ in range(6):
            add_message(s,c,'ai','您还想了解什么？')
            add_message(s,c,'visitor','先了解一下')
    captured=[]
    def generated(cfg,company,history,records):
        captured.extend(history);return '可以继续梳理栏目范围。',[]
    monkeypatch.setattr(gateway,'grounded_answer',generated)
    send(t,v,cid,'预算改为5万元')
    context=dialogue_context(captured)
    assert len(captured)>9 and context['budget_changed']
    assert context['user_requirements']['materials']=='ready'


def test_new_history_window_still_excludes_future_customer_messages(make_tenant,make_visitor,monkeypatch):
    t=make_tenant();v,cid=make_visitor(t);enable(t)
    v.post('/api/public/conversations/'+cid+'/messages',json={'text':'官网预算3万元','client_id':'earlier'})
    with transaction(t.id) as s:
        c=s.get(Conversation,(t.id,cid));m=add_message(s,c,'visitor','预算改为8万元',client_id='later')
        s.add(AIJob(tenant_id=t.id,conversation_id=cid,message_id=m.id))
    captured=[]
    def generated(cfg,company,history,records):
        captured.extend(history);return '可以先梳理建设范围。',[]
    monkeypatch.setattr(gateway,'grounded_answer',generated)
    run_ai(t.id,lease_ai(t.id))
    assert all('8万元' not in m['content'] for m in captured)
