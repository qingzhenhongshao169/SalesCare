"""Tenant-specific outage protection must preserve knowledge, budget and recovery."""
import time
from mvp.db import transaction, rows, database_time, serialization_lock
from mvp.models import Usage, ModelConfig
from mvp.ai_resilience import model_health, CUSTOMER_WAIT_SECONDS
from server.model_gateway import DEFAULT, fingerprint
from test_conversation import enable, send


def failures(t, age=2, fp=None):
    with transaction(t.id) as s:
        cfg={**DEFAULT,**s.get(ModelConfig,(t.id,'primary')).config}
        fp=fp or fingerprint(cfg)
        for offset in (1,0):
            s.add(Usage(tenant_id=t.id,kind='llm',tokens=0,outcome='fallback_timeout',
                        latency_ms=1000,created_at=time.time()-age-offset,
                        detail={'model_fingerprint':fp}))
    return fp


def test_repeated_timeouts_skip_call_and_keep_knowledge_answer(make_tenant,make_visitor,monkeypatch):
    t=make_tenant();v,cid=make_visitor(t);enable(t);failures(t)
    monkeypatch.setattr('mvp.worker.model_gateway.grounded_answer',lambda *args:__import__('pytest').fail('Open circuit called model'))
    reply=send(t,v,cid,'企业官网建设需要准备什么？')
    assert reply['text'] and reply['citations']
    with transaction(t.id) as s:
        u=s.scalar(rows(s,Usage,t.id).where(Usage.detail['conversation_id'].as_string()==cid))
        assert u.outcome=='fallback_circuit_open' and u.kind=='retrieval' and u.tokens==0
        assert not u.detail['budget_blocked'] and u.detail['model_health']['state']=='cooldown'


def test_model_timeout_is_capped_only_for_customer_generation(make_tenant,make_visitor,monkeypatch):
    t=make_tenant();v,cid=make_visitor(t);enable(t);observed=[]
    def answer(cfg,name,history,records):
        observed.append(cfg['timeout']);return '可以先提供公司介绍和产品资料。',records
    monkeypatch.setattr('mvp.worker.model_gateway.grounded_answer',answer)
    reply=send(t,v,cid,'企业官网需要准备什么？')
    assert reply['text']=='可以先提供公司介绍和产品资料。'
    assert observed and observed[0]<=CUSTOMER_WAIT_SECONDS
    with transaction(t.id) as s:assert s.get(ModelConfig,(t.id,'primary')).config.get('timeout',20)==20


def test_outage_does_not_cross_tenant_or_model_settings(make_tenant,make_visitor,monkeypatch):
    a=make_tenant();b=make_tenant();enable(a);enable(b);v,cid=make_visitor(b)
    failures(a);failures(b,fp='different-model-fingerprint')
    called=[]
    monkeypatch.setattr('mvp.worker.model_gateway.grounded_answer',lambda *args:called.append(True) or ('可以提供企业介绍。',args[3]))
    send(b,v,cid,'企业官网需要准备什么？')
    assert called


def test_after_cooldown_one_probe_is_allowed_and_concurrent_probe_is_blocked(make_tenant):
    t=make_tenant();fp=failures(t,age=45)
    with transaction(t.id) as s:
        serialization_lock(s,t.id,'ai-usage')
        assert model_health(s,t.id,fp,database_time(s))=={'state':'probe','allow_call':True}
        s.add(Usage(tenant_id=t.id,kind='llm',outcome='reserved',detail={'model_fingerprint':fp}));s.flush()
        assert model_health(s,t.id,fp,database_time(s))=={'state':'probe_in_progress','allow_call':False}


def test_success_resets_consecutive_failure_history(make_tenant):
    t=make_tenant();fp=failures(t)
    with transaction(t.id) as s:
        s.add(Usage(tenant_id=t.id,kind='llm',outcome='llm',latency_ms=800,detail={'model_fingerprint':fp}));s.flush()
        assert model_health(s,t.id,fp,database_time(s))=={'state':'available','allow_call':True}


def test_content_validation_failure_does_not_mark_provider_offline(make_tenant):
    t=make_tenant();fp=failures(t)
    with transaction(t.id) as s:
        s.add(Usage(tenant_id=t.id,kind='llm',outcome='fallback_citations',latency_ms=1000,detail={'model_fingerprint':fp}));s.flush()
        assert model_health(s,t.id,fp,database_time(s))['allow_call']
