import json
import pytest
from sqlalchemy import select
from server import model_gateway as gateway
from server.answer_policy import numbers_supported
from mvp.answering import contextual_fallback
from mvp.db import transaction, rows
from mvp.models import Knowledge, Usage
from test_conversation import enable, send

HISTORY=[{'role':'user','content':'网站建设500页面多少钱'},
         {'role':'assistant','content':'请说明预算与时间'},
         {'role':'user','content':'没有网站，核心功能展示公司，希望2周上线，预算1万'}]

@pytest.mark.parametrize('answer',[
    '您的预算是1万元，希望2周上线。500页面是指独立设计还是内容条数？',
    '您的预算是10,000元，您期望两周上线。',
    '您说的500个页面是指独立设计，还是内容条数？',
    '您说的500页面，是指500个独立设计的页面，还是500条内容？',
    '您提出预算1万元，具体报价仍需核算；希望2周上线，排期也需要确认。',
    '目前需求是500条产品内容，预算一万元、希望两周上线。',
])
def test_customer_quantities_can_be_rephrased_without_company_evidence(answer):
    assert numbers_supported(answer,'按范围评估',HISTORY)

@pytest.mark.parametrize('answer',[
    '我们报价1万元，2周能上线。',
    '您的预算1万元足够，我们能做。',
    '您预算1万，收费1万元。',
    '您的预算是2万元。',
    '您希望3周上线。',
    '您的预算1万就能做500个页面。',
    '您的500页面预算是500元吗？',
    '您的预算1万元 就能做500个页面。',
])
def test_customer_numbers_do_not_authorize_price_deadline_or_changed_requirement(answer):
    assert not numbers_supported(answer,'按范围评估',HISTORY)

def test_assistant_hallucination_does_not_authorize_numbers():
    assert not numbers_supported('您的预算999元。','',[
        {'role':'user','content':'想做官网'},
        {'role':'assistant','content':'您预算999元'}])

def test_published_numeric_quote_keeps_original_path():
    assert numbers_supported('标准版199元。','标准版199元。',[])

def test_same_screenshot_fallback_remembers_details_and_asks_only_ambiguity():
    history=[{**HISTORY[0],'content':'网站建设500页面多少钱'},HISTORY[1],
             {**HISTORY[2],'content':'网站建设 没有网站 核心功能展示公司 上线时间要尽快2周 预算1万'}]
    answer,used=contextual_fallback([{'content':'为了报价请提供预算、时间和功能。'}],history)
    assert '预算1万' in answer and '2周' in answer and '500页面' in answer
    assert '独立设计' in answer and answer.count('？')==1 and '请提供预算' not in answer
    assert used==[]

def test_fallback_does_not_reask_resolved_page_ambiguity():
    answer,_=contextual_fallback([],HISTORY+[{'role':'user','content':'是500条新闻，不是独立页面'}])
    assert '还是' not in answer and '资料' in answer

def test_fallback_does_not_reask_available_materials():
    answer,_=contextual_fallback([],HISTORY+[{'role':'user','content':'是500条产品，不是独立页面，资料都有了'}])
    assert '既然资料已备齐' in answer and '现成资料吗' not in answer

def test_model_numeric_rephrasing_is_accepted_with_real_citation(monkeypatch):
    records=[{'id':'faq','title':'官网报价','content':'按项目范围评估报价。','version':1}]
    monkeypatch.setattr(gateway,'complete',lambda *_:json.dumps({
        'answer':'您的预算是一万元，希望两周上线。500页面是指独立设计还是内容条数？',
        'knowledge_ids':['faq']},ensure_ascii=False))
    answer,used=gateway.grounded_answer(gateway.DEFAULT,'测试企业',HISTORY,records)
    assert '一万元' in answer and used==records

def test_model_without_records_can_analyze_but_cannot_invent_company_capability(monkeypatch):
    monkeypatch.setattr(gateway,'complete',lambda *_:json.dumps({
        'answer':'可以先梳理展示目标与内容结构。您是否有现成的图文资料？','knowledge_ids':[]},ensure_ascii=False))
    assert gateway.grounded_answer(gateway.DEFAULT,'测试企业',HISTORY,[])[1]==[]
    monkeypatch.setattr(gateway,'complete',lambda *_:json.dumps({
        'answer':'我们提供免费建站服务。','knowledge_ids':[]},ensure_ascii=False))
    with pytest.raises(gateway.ProviderError):gateway.grounded_answer(gateway.DEFAULT,'测试企业',HISTORY,[])

def test_empty_retrieval_still_calls_model_for_general_advice(make_tenant,make_visitor,monkeypatch):
    t=make_tenant();v,cid=make_visitor(t);enable(t)
    with transaction(t.id) as s:s.get(Knowledge,(t.id,'faq')).status='withdrawn'
    calls=[]
    def complete(cfg,messages):
        calls.append(messages)
        return json.dumps({'answer':'可以先明确目标用户，再梳理内容结构。您希望访客完成什么行动？','knowledge_ids':[]},ensure_ascii=False)
    monkeypatch.setattr(gateway,'complete',complete)
    reply=send(t,v,cid,'想做企业官网，怎么组织内容？')
    assert calls and '目标用户' in reply['text'] and reply['citations']==[]
    with transaction(t.id) as s:assert s.scalar(rows(s,Usage,t.id)).outcome=='llm'

def test_provider_failure_uses_customer_context_not_source_copy(make_tenant,make_visitor,monkeypatch):
    t=make_tenant();v,cid=make_visitor(t);enable(t)
    def failed(*_):raise gateway.ProviderError('模型引入了未核实数字')
    monkeypatch.setattr(gateway,'grounded_answer',failed)
    send(t,v,cid,HISTORY[0]['content'],'first')
    reply=send(t,v,cid,'网站建设，没有网站，展示公司，希望2周上线，预算1万','second')
    assert '预算1万' in reply['text'] and '2周' in reply['text'] and '500页面' in reply['text']
    assert '独立设计' in reply['text'] and reply['text'].count('？')==1
