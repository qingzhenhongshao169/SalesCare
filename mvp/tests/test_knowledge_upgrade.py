"""Synthetic data only; real isolated PostgreSQL and mocked model boundaries."""
import time
import pytest
from sqlalchemy import select
from mvp.db import transaction, rows
from mvp.models import (Knowledge, KBDocument, KBDocumentVersion, KBChunk, KBJob, KBEngineConfig,
                        Usage, AIJob, Conversation, Channel, uid)
from mvp.knowledge_documents import process
from mvp.knowledge_worker import lease
from mvp.knowledge_engine import EngineError, fingerprint
from mvp.knowledge_retrieval import search, revalidate
from mvp.worker import run_ai, lease_ai
from mvp.agent_flow import ReceptionFlow, FlowExpired
from test_document_knowledge import ready
from test_conversation import send, enable


def pending(t, client, content='产品T100支持清洗滤网。'):
    collection=client.get('/api/kb/collections').json()[0]['id']
    r=client.post('/api/kb/documents/upload',data={'collection_id':collection,'visibility':'public'},
                  files={'file':('产品.md',content.encode())})
    assert r.status_code==200
    return r.json()


def test_manifest_records_real_build_and_checks_corruption(make_tenant,make_employee):
    t=make_tenant();a,_=make_employee(t);d,_=ready(t,a,visibility='public')
    view=a.get('/api/kb/documents/'+d['document_id']).json()['versions'][0]
    assert view['build']['tenant_id']==t.id and view['build']['dimension']==0
    assert view['build']['chunk_count']==view['chunk_count'] and len(view['build']['generation'])==64
    with transaction(t.id) as s:s.scalar(rows(s,KBChunk,t.id)).content='被意外改写的内容'
    assert a.post('/api/kb/versions/'+d['version_id']+'/publish',json={}).status_code==409


def test_model_switch_requires_rebuild_before_publication(make_tenant,make_employee):
    t=make_tenant();a,_=make_employee(t);d,_=ready(t,a,visibility='public')
    with transaction(t.id) as s:
        cfg=s.get(KBEngineConfig,(t.id,'primary'));cfg.config={**cfg.config,'embedding_model':'different-model'}
    assert a.post('/api/kb/versions/'+d['version_id']+'/publish',json={}).status_code==409
    assert fingerprint({'embedding_mode':'local','embedding_model':'A'})!=fingerprint({'embedding_mode':'local','embedding_model':'B'})


def test_replaced_file_citation_and_trace_use_real_version(make_tenant,make_employee,make_visitor):
    t=make_tenant();a,_=make_employee(t);v,cid=make_visitor(t)
    d,col=ready(t,a,content='产品T100支持清洗滤网。',visibility='public')
    a.post('/api/kb/versions/'+d['version_id']+'/publish',json={})
    new=a.post('/api/kb/documents/upload',data={'collection_id':col,'document_id':d['document_id']},
               files={'file':('产品.md','产品T100的滤网需取下清洗，晾干后装回。'.encode())}).json()
    process(t.id,*lease(t.id,with_token=True))
    assert a.post('/api/kb/versions/'+new['version_id']+'/publish',json={}).status_code==200
    r=send(t,v,cid,'T100滤网怎么清洗？')
    assert r['citations'] and r['citations'][0]['version']==2
    diagnostic=a.get('/api/conversations/'+cid).json()['answer_diagnostics'][0]['detail']
    assert diagnostic['used'][0]['version_id']==new['version_id']
    assert [s['stage'] for s in diagnostic['flow']['steps']]==['route','retrieve','verify','deliver']
    assert diagnostic['flow']['generation_calls']==0
    assert 'answer_diagnostics' not in v.get('/api/public/conversations/'+cid).json()


def test_expired_or_orphan_document_is_not_searchable(make_tenant,make_employee,make_visitor):
    t=make_tenant();a,_=make_employee(t);v,_=make_visitor(t);d,_=ready(t,a,visibility='public')
    a.post('/api/kb/versions/'+d['version_id']+'/publish',json={})
    with transaction(t.id) as s:
        chunk=s.scalar(rows(s,KBChunk,t.id));kid=chunk.id
        s.get(Knowledge,(t.id,kid)).expires_at=time.time()-1
        s.add(Knowledge(tenant_id=t.id,family='orphan',id='orphan',category='文档知识',title='产品ABC',
                        content='没有原始分块的错误公开内容',keywords=['ABC'],status='published',author_id='test'))
    found,_=search(t.id,None,'ABC是什么？')
    assert not found and v.get('/api/public/document-source/'+kid).status_code==404


def test_candidates_rechecked_against_tenant_and_content(make_tenant,make_employee):
    one=make_tenant();two=make_tenant();a,_=make_employee(one)
    d,_=ready(one,a,visibility='public');a.post('/api/kb/versions/'+d['version_id']+'/publish',json={})
    records,_=search(one.id,None,'ABC工业设备')
    assert records
    with transaction(two.id) as s:assert not revalidate(s,two.id,records)
    with transaction(one.id) as s:
        changed=[{**r,'content':r['content']+'伪造外部索引正文'} for r in records]
        assert not revalidate(s,one.id,changed)


def test_streaming_candidates_are_bounded_and_keep_lexical_winner(make_tenant):
    t=make_tenant()
    with transaction(t.id) as s:
        for i in range(340):
            s.add(Knowledge(tenant_id=t.id,id=f'doc-{i:04}',family=f'family-{i}',title='合成产品资料',
                content='介绍一般产品的使用方法。' if i!=339 else '星叶滤网支持水洗，晾干后重新安装。',
                keywords=['滤网','水洗'] if i==339 else [],status='published',author_id='test'))
    found,diag=search(t.id,None,'滤网支持水洗吗？')
    assert found[0]['id']=='doc-0339'
    assert diag['scanned']==341 and diag['candidates']<=diag['candidate_limit']
    assert diag['max_retained_candidates']==2*diag['candidate_limit']
    assert diag['batch_size']==128


def test_semantic_scan_keeps_no_keyword_match_and_rerank_falls_back(make_tenant,make_employee,monkeypatch):
    t=make_tenant();a,_=make_employee(t)
    cfg={'embedding_mode':'api','embedding_model':'synthetic','rerank_mode':'api','candidate_limit':4}
    with transaction(t.id) as s:s.add(KBEngineConfig(tenant_id=t.id,id='primary',config=cfg))
    monkeypatch.setattr('mvp.knowledge_documents.embed',lambda texts,*_: [[1.]+[0.]*7 for _ in texts])
    d=pending(t,a,'产品说明：拆下滤网冲洗，晾干后装回。');process(t.id,*lease(t.id,with_token=True))
    assert a.post('/api/kb/versions/'+d['version_id']+'/publish',json={}).status_code==200
    calls=[]
    monkeypatch.setattr('mvp.knowledge_retrieval.embed',lambda *args,**kw: calls.append(kw) or [[1.]+[0.]*7])
    found,diag=search(t.id,None,'清洁过滤部件')
    assert found and len(calls)==1 and diag['embedding']=='vector' and diag['rerank']=='fusion_fallback'
    # Same configured provider returning a new dimension must never be mixed.
    monkeypatch.setattr('mvp.knowledge_retrieval.embed',lambda *args,**kw:[[1.]+[0.]*15])
    found,diag=search(t.id,None,'清洁过滤部件')
    assert diag['vector_dimension_mismatches']==1 and not found


def test_uncited_input_withdrawal_invalidates_whole_model_answer(make_tenant,make_visitor,monkeypatch):
    t=make_tenant();v,cid=make_visitor(t);enable(t)
    with transaction(t.id) as s:
        s.add(Knowledge(tenant_id=t.id,id='other',family='other',title='企业官网准备资料',
            content='企业官网准备资料需要提供公司介绍和产品照片。',keywords=['官网','准备','资料'],status='published',author_id='test'))
    def generate(cfg,name,history,records):
        assert len(records)>=2
        with transaction(t.id) as s:s.get(Knowledge,(t.id,records[1]['id'])).status='withdrawn'
        return '包含已撤回输入影响的回答',records[:1]
    monkeypatch.setattr('mvp.worker.model_gateway.grounded_answer',generate)
    r=send(t,v,cid,'企业官网需要准备什么资料？')
    assert r['text']!='包含已撤回输入影响的回答'
    with transaction(t.id) as s:
        usage=s.scalar(rows(s,Usage,t.id));assert usage.outcome=='fallback_changed'
        assert usage.detail['evidence_check']['withdrawn_or_changed']


def test_document_scope_change_during_generation_is_not_delivered(make_tenant,make_employee,make_visitor,monkeypatch):
    t=make_tenant();a,_=make_employee(t);v,cid=make_visitor(t);enable(t)
    d,_=ready(t,a,content='产品T100支持清洗滤网。',visibility='public')
    a.post('/api/kb/versions/'+d['version_id']+'/publish',json={})
    def generate(*args,**kwargs):
        with transaction(t.id) as s:s.get(KBDocument,(t.id,d['document_id'])).channel_ids=['a-different-channel']
        return '不应发送的受限资料',args[3]
    monkeypatch.setattr('mvp.worker.model_gateway.grounded_answer',generate)
    r=send(t,v,cid,'T100滤网如何清洗？')
    assert r['text']!='不应发送的受限资料' and not r['citations']


def test_stale_ai_attempt_cannot_finish_released_job(make_tenant,make_visitor,monkeypatch):
    t=make_tenant();v,cid=make_visitor(t);enable(t)
    v.post('/api/public/conversations/'+cid+'/messages',json={'text':'企业官网需要什么资料？','client_id':'fence'})
    old=lease_ai(t.id,with_token=True);new=[]
    def generate(*args):
        with transaction(t.id) as s:s.get(AIJob,(t.id,old[0])).lease_until=time.time()-1
        new.extend(lease_ai(t.id,with_token=True))
        return '旧任务不可发送',args[3]
    monkeypatch.setattr('mvp.worker.model_gateway.grounded_answer',generate)
    run_ai(t.id,*old)
    with transaction(t.id) as s:
        j=s.get(AIJob,(t.id,old[0]));assert j.status=='leased' and j.lease_token==new[1]
    assert not any(m['text']=='旧任务不可发送' for m in v.get('/api/public/conversations/'+cid).json()['messages'])
    monkeypatch.setattr('mvp.worker.model_gateway.grounded_answer',lambda *args:('可以提供公司介绍和产品资料。',args[3]))
    run_ai(t.id,*new)
    assert v.get('/api/public/conversations/'+cid).json()['messages'][-1]['text']=='可以提供公司介绍和产品资料。'


def test_pending_handoff_still_answers_and_channel_disable_suppresses(make_tenant,make_visitor,monkeypatch):
    t=make_tenant();v,cid=make_visitor(t)
    assert v.post('/api/public/conversations/'+cid+'/handoff',json={}).status_code==200
    assert send(t,v,cid,'企业官网需要什么资料？')['citations']
    enable(t)
    with transaction(t.id) as s:
        ch=Channel(tenant_id=t.id,name='测试渠道',kind='web',enabled=True,config={});s.add(ch);s.flush()
        s.get(Conversation,(t.id,cid)).channel_id=ch.id;channel_id=ch.id
    def generate(*args):
        with transaction(t.id) as s:s.get(Channel,(t.id,channel_id)).enabled=False
        return '已停用渠道不应投递',args[3]
    monkeypatch.setattr('mvp.worker.model_gateway.grounded_answer',generate)
    v.post('/api/public/conversations/'+cid+'/messages',json={'text':'官网资料还要什么？','client_id':'disabled'})
    job=lease_ai(t.id)
    assert job
    run_ai(t.id,job)
    with transaction(t.id) as s:assert s.get(AIJob,(t.id,job)).status=='cancelled'


@pytest.mark.parametrize('code',['provider_401','provider_format','embedding_input_limit'])
def test_permanent_build_errors_do_not_retry_forever(make_tenant,make_employee,monkeypatch,code):
    t=make_tenant();a,_=make_employee(t);d=pending(t,a)
    monkeypatch.setattr('mvp.knowledge_documents.embed',lambda *args:(_ for _ in ()).throw(EngineError(code)))
    process(t.id,*lease(t.id,with_token=True))
    version=a.get('/api/kb/documents/'+d['document_id']).json()['versions'][0]
    assert version['status']=='failed' and version['job']['attempts']==1 and lease(t.id) is None


def test_transient_build_errors_stop_after_three_and_manual_retry_rotates_token(make_tenant,make_employee,monkeypatch):
    t=make_tenant();a,_=make_employee(t);d=pending(t,a)
    monkeypatch.setattr('mvp.knowledge_documents.embed',lambda *args:(_ for _ in ()).throw(EngineError('provider_503')))
    tokens=[]
    for attempt in range(1,4):
        jid,token=lease(t.id,with_token=True);tokens.append(token);process(t.id,jid,token)
        with transaction(t.id) as s:
            j=s.get(KBJob,(t.id,jid));assert j.attempts==attempt
            assert j.status==('pending' if attempt<3 else 'failed')
            if attempt<3:
                assert j.lease_until>time.time();j.lease_until=0
    assert len(set(tokens))==3
    assert a.post('/api/kb/versions/'+d['version_id']+'/retry',json={}).status_code==200
    current=lease(t.id,with_token=True)
    process(t.id,jid,tokens[0])
    with transaction(t.id) as s:
        j=s.get(KBJob,(t.id,jid));assert j.status=='leased' and j.lease_token==current[1]


def test_lease_exhaustion_marks_version_failed(make_tenant,make_employee):
    t=make_tenant();a,_=make_employee(t);d=pending(t,a)
    for _ in range(3):
        jid=lease(t.id)
        with transaction(t.id) as s:s.get(KBJob,(t.id,jid)).lease_until=0
    assert lease(t.id) is None
    view=a.get('/api/kb/documents/'+d['document_id']).json()['versions'][0]
    assert view['status']=='failed' and view['job']['status']=='failed'


def test_old_ingestion_result_does_not_overwrite_reclaimed_attempt(make_tenant,make_employee,monkeypatch):
    t=make_tenant();a,_=make_employee(t);d=pending(t,a);old=lease(t.id,with_token=True);new=[]
    def embedding(texts,*args):
        with transaction(t.id) as s:s.get(KBJob,(t.id,old[0])).lease_until=0
        new.extend(lease(t.id,with_token=True))
        return [[1.]+[0.]*7 for _ in texts]
    monkeypatch.setattr('mvp.knowledge_documents.embed',embedding)
    process(t.id,*old)
    with transaction(t.id) as s:
        j=s.get(KBJob,(t.id,old[0]));assert j.lease_token==new[1] and j.status=='leased'
        assert not list(s.scalars(rows(s,KBChunk,t.id)))


def test_flow_deadline_and_one_generation_limit():
    flow=ReceptionFlow(time.time()+30)
    with flow.step('route'):pass
    with flow.step('generate'):pass
    with pytest.raises(RuntimeError):
        with flow.step('generate'):pass
    expired=ReceptionFlow(time.time()-1)
    with pytest.raises(FlowExpired):expired.check()


def test_revalidation_holds_document_authorization_until_reply_commit(make_tenant,make_employee):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event
    from mvp.knowledge_documents import withdraw
    t=make_tenant();a,member=make_employee(t);d,_=ready(t,a,visibility='public')
    a.post('/api/kb/versions/'+d['version_id']+'/publish',json={})
    records,_=search(t.id,None,'ABC是什么设备？');assert records
    started=Event()
    def revoke():
        started.set();withdraw({'tenant':t.id,'id':member.id},d['document_id'])
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(t.id) as s:
            assert revalidate(s,t.id,records,lock=True)
            future=pool.submit(revoke);assert started.wait(2)
            with pytest.raises(TimeoutError):future.result(timeout=.1)
        future.result(timeout=5)
    assert not search(t.id,None,'ABC是什么设备？')[0]


def test_retry_success_does_not_publish_replacement_automatically(make_tenant,make_employee,monkeypatch):
    t=make_tenant();a,_=make_employee(t);old,col=ready(t,a,content='产品T100支持滤网清洗。',visibility='public')
    a.post('/api/kb/versions/'+old['version_id']+'/publish',json={})
    new=a.post('/api/kb/documents/upload',data={'collection_id':col,'document_id':old['document_id']},
               files={'file':('产品.md','产品T100滤网需要晾干后使用。'.encode())}).json()
    monkeypatch.setattr('mvp.knowledge_documents.embed',lambda *args:(_ for _ in ()).throw(EngineError('provider_connection')))
    jid=lease(t.id);process(t.id,jid)
    with transaction(t.id) as s:
        assert s.get(KBDocument,(t.id,old['document_id'])).active_version==old['version_id']
        s.get(KBJob,(t.id,jid)).lease_until=0
    monkeypatch.setattr('mvp.knowledge_documents.embed',lambda texts,*args:[[] for _ in texts])
    process(t.id,*lease(t.id,with_token=True))
    with transaction(t.id) as s:
        assert s.get(KBDocumentVersion,(t.id,new['version_id'])).status=='ready'
        assert s.get(KBDocument,(t.id,old['document_id'])).active_version==old['version_id']


def test_changed_model_skips_old_vectors_without_hiding_public_knowledge(make_tenant,make_employee,monkeypatch):
    t=make_tenant();a,_=make_employee(t);d,_=ready(t,a,visibility='public')
    a.post('/api/kb/versions/'+d['version_id']+'/publish',json={})
    with transaction(t.id) as s:
        cfg=s.get(KBEngineConfig,(t.id,'primary'));cfg.config={'embedding_mode':'api','embedding_model':'new-model','rerank_mode':'off'}
    monkeypatch.setattr('mvp.knowledge_retrieval.embed',lambda *args,**kw:pytest.fail('Old index must not cause embedding request'))
    found,diag=search(t.id,None,'ABC工业设备')
    assert found and diag['stale_index_chunks']==1 and diag['embedding']=='keyword'


def test_model_execution_over_deadline_is_suppressed(make_tenant,make_visitor,monkeypatch):
    t=make_tenant();v,cid=make_visitor(t);enable(t);flows=[]
    from mvp import worker
    def create_flow(lease_until):
        flow=ReceptionFlow(lease_until);flows.append(flow);return flow
    monkeypatch.setattr(worker,'ReceptionFlow',create_flow)
    def generate(*args):
        flows[0].deadline=time.monotonic()-1
        return '超时回答不可发送',args[3]
    monkeypatch.setattr('mvp.worker.model_gateway.grounded_answer',generate)
    v.post('/api/public/conversations/'+cid+'/messages',json={'text':'企业官网需要什么资料？','client_id':'timeout'})
    jid=lease_ai(t.id);run_ai(t.id,jid)
    with transaction(t.id) as s:
        assert s.get(AIJob,(t.id,jid)).lease_until==0
        assert s.scalar(rows(s,Usage,t.id)).outcome=='suppressed_timeout'
    assert not any(m['text']=='超时回答不可发送' for m in v.get('/api/public/conversations/'+cid).json()['messages'])
