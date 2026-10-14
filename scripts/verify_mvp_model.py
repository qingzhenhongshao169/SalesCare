"""Two explicitly synthetic live messages; uses configured provider quota, prints no credentials."""
import json
from pathlib import Path
import sys
import time
import uuid
import httpx
from sqlalchemy import select
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mvp.db import transaction,rows
from mvp.models import Tenant,ModelConfig,Conversation,Usage
from mvp.service import audit
from mvp.answering import social_fallback

with transaction() as s:tenant=s.scalar(select(Tenant).where(Tenant.slug=='biaofun'))
with transaction(tenant.id) as s:
    cfg=s.get(ModelConfig,(tenant.id,'primary')).config
    if not cfg.get('enabled'):raise SystemExit('Administrator must enable the tested configuration first.')
    model=cfg.get('model');protocol=cfg.get('protocol')
start=time.time();result={'model':model,'protocol':protocol,'synthetic':True,'turns':[]}
with httpx.Client(base_url='http://127.0.0.1:8767',trust_env=False,timeout=10) as client:
    response=client.post('/api/public/session/biaofun');response.raise_for_status();session=response.json()
    client.headers.update({'Authorization':'Bearer '+session['token'],'X-CSRF-Token':session['csrf']})
    response=client.post('/api/public/conversations',json={});response.raise_for_status();cid=response.json()['id']
    with transaction(tenant.id) as s:
        s.get(Conversation,(tenant.id,cid)).name='模型联调 · 内部测试'
        audit(s,tenant.id,'local-operator','verification.consultation',cid,{'synthetic':True})
    for question in ['你好','我想做一个企业官网，前期需要准备哪些资料？']:
        at=time.monotonic()
        response=client.post('/api/public/conversations/'+cid+'/messages',json={'text':question,'client_id':'model-check-'+uuid.uuid4().hex})
        response.raise_for_status();sequence=response.json()['message']['sequence']
        reply=None
        while time.monotonic()-at<55:
            response=client.get('/api/public/conversations/'+cid);response.raise_for_status()
            replies=[m for m in response.json()['messages'] if m['role']=='ai' and m['sequence']>sequence]
            if replies:reply=replies[0];break
            time.sleep(1)
        if not reply:raise SystemExit('No reply within the smoke-test window; check the durable job and worker.')
        result['turns'].append({'question':question,'answer':reply['text'],'citations':reply['citations'],'elapsed_ms':round((time.monotonic()-at)*1000)})
with transaction(tenant.id) as s:
    result['usage_in_window']=[{'kind':u.kind,'outcome':u.outcome,'latency_ms':u.latency_ms} for u in s.scalars(rows(s,Usage,tenant.id).where(Usage.created_at>=start))]
result['greeting_not_generic_fallback']=result['turns'][0]['answer']!=social_fallback('greeting',[{'role':'user','content':'你好'}])
result['scope']='Two synthetic live turns, not a business-quality or full-provider evaluation; usage window may include concurrent consultations.'
(ROOT/'docs/mvp-model-live-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
print(json.dumps(result,ensure_ascii=False))
