import asyncio
import threading
import time
import pytest
from sqlalchemy import select
from mvp.db import transaction,rows
from mvp.models import Knowledge,Usage,Conversation,Channel,uid
from mvp.realtime import PgListener,MessageBus,JOB_CHANNEL,MESSAGE_CHANNEL,conversation_key
from mvp.service import add_message
from test_conversation import enable,send


def approve(client,question='官网建设要准备什么资料？',answer='可以先提供企业介绍、产品服务资料、参考网站和希望上线的时间。'):
    return client.put('/api/quick-replies/faq',json={'version':1,'questions':[question],'answer':answer,'confirmed':True})


def test_pg_wake_is_commit_only_and_rollback_does_not_leak(make_tenant,make_visitor):
    t=make_tenant();v,cid=make_visitor(t);key=conversation_key(t.id,cid);notified=threading.Event()
    listener=PgListener(MESSAGE_CHANNEL,lambda payload:notified.set() if payload==key else None)
    listener.start();assert listener.ready.wait(3)
    try:
        with pytest.raises(RuntimeError):
            with transaction(t.id) as s:
                add_message(s,s.get(Conversation,(t.id,cid)),'ai','回滚消息')
                assert not notified.wait(.05)
                raise RuntimeError('synthetic rollback')
        assert not notified.wait(.1)
        with transaction(t.id) as s:
            add_message(s,s.get(Conversation,(t.id,cid)),'ai','已提交消息')
            assert not notified.wait(.05)
        assert notified.wait(2)
        assert t.id not in key and cid not in key
    finally:listener.close()


def test_web_message_wakes_ai_lease_without_polling(make_tenant,make_visitor):
    t=make_tenant();v,cid=make_visitor(t);wake=threading.Event()
    listener=PgListener(JOB_CHANNEL,lambda _:wake.set());listener.start();assert listener.ready.wait(3)
    try:
        assert v.post('/api/public/conversations/'+cid+'/messages',json={'text':'你好','client_id':'wake'}).status_code==200
        assert wake.wait(2)
    finally:listener.close()


def test_message_bus_routes_only_matching_conversation(make_tenant,make_visitor):
    t=make_tenant();v,cid=make_visitor(t);bus=MessageBus()
    async def check():
        key,signal=bus.subscribe(t.id,cid);other,wrong=bus.subscribe(t.id,'different')
        assert await asyncio.to_thread(bus.listener.ready.wait,3)
        try:
            with transaction(t.id) as s:add_message(s,s.get(Conversation,(t.id,cid)),'ai','唤醒正确会话')
            await asyncio.wait_for(signal.wait(),2)
            assert not wrong.is_set()
        finally:bus.unsubscribe(key,signal);bus.unsubscribe(other,wrong);bus.listener.close()
    asyncio.run(check())


def test_exact_reviewed_faq_skips_provider_and_returns_real_citation(make_tenant,make_employee,make_visitor,monkeypatch):
    t=make_tenant();a,_=make_employee(t);v,cid=make_visitor(t);enable(t)
    assert approve(a).status_code==200
    monkeypatch.setattr('mvp.worker.model_gateway.grounded_answer',lambda *args:pytest.fail('FAQ must not call provider'))
    reply=send(t,v,cid,'官网建设要准备什么资料？')
    assert reply['citations'][0]['id']=='faq' and '企业介绍' in reply['text']
    with transaction(t.id) as s:
        usage=s.scalar(rows(s,Usage,t.id));assert usage.kind=='faq' and usage.outcome=='faq' and usage.tokens==0


@pytest.mark.parametrize('question',['预算只有5000元，官网建设要准备什么资料？','刚才那个官网建设要准备什么资料？','官网建设要准备什么资料？还能保证两周交付吗？'])
def test_personalized_or_changed_question_does_not_hit_faq(make_tenant,make_employee,make_visitor,question):
    t=make_tenant();a,_=make_employee(t);v,cid=make_visitor(t);approve(a)
    send(t,v,cid,question)
    with transaction(t.id) as s:assert s.scalar(rows(s,Usage,t.id)).outcome!='faq'


def test_faq_requires_publisher_and_supported_facts(make_tenant,make_employee):
    t=make_tenant();a,_=make_employee(t);editor,_=make_employee(t,['editor'])
    assert approve(editor).status_code==403
    assert approve(a,answer='报价是100元。').status_code==422
    assert approve(a,answer='先确认客户问的是哪个产品，再引用本型号。').status_code==422
    assert a.put('/api/quick-replies/faq',json={'version':1,'questions':['问题'],'answer':'公开回答'}).status_code==422


@pytest.mark.parametrize('change',['withdraw','expire','scope','duplicate','content'])
def test_faq_never_uses_stale_unauthorized_or_ambiguous_source(make_tenant,make_employee,make_visitor,change):
    t=make_tenant();a,_=make_employee(t);v,cid=make_visitor(t);approve(a)
    with transaction(t.id) as s:
        k=s.get(Knowledge,(t.id,'faq'))
        if change=='withdraw':k.status='withdrawn'
        elif change=='expire':k.expires_at=time.time()-1
        elif change=='scope':k.visibility='internal'
        elif change=='content':k.content+='内容发生变化'
        else:s.add(Knowledge(tenant_id=t.id,id='duplicate',family='duplicate',title=k.title,content=k.content,
                             status='published',author_id='test',quick_reply=dict(k.quick_reply)))
    send(t,v,cid,'官网建设要准备什么资料？')
    with transaction(t.id) as s:assert s.scalar(rows(s,Usage,t.id)).outcome!='faq'


def test_foreign_tenant_cannot_configure_or_consume_faq(make_tenant,make_employee,make_visitor):
    t=make_tenant();other=make_tenant();a,_=make_employee(t);b,_=make_employee(other);approve(a)
    v,cid=make_visitor(other);send(other,v,cid,'官网建设要准备什么资料？')
    with transaction(other.id) as s:assert s.scalar(rows(s,Usage,other.id)).outcome!='faq'
    assert b.put('/api/quick-replies/foreign-id',json={'version':1,'enabled':False}).status_code==404


def test_faq_changed_during_preparation_is_not_sent(make_tenant,make_employee,make_visitor,monkeypatch):
    from mvp import worker
    t=make_tenant();a,_=make_employee(t);v,cid=make_visitor(t);approve(a)
    original=worker.quick_lookup
    def changed(*args):
        result=original(*args)
        with transaction(t.id) as s:s.get(Knowledge,(t.id,'faq')).status='withdrawn'
        return result
    monkeypatch.setattr(worker,'quick_lookup',changed)
    reply=send(t,v,cid,'官网建设要准备什么资料？')
    assert not reply['citations']
    with transaction(t.id) as s:assert s.scalar(rows(s,Usage,t.id)).outcome=='fallback_changed'
