"""Four synthetic website turns against the existing tenant model budget. No credentials output."""
import json
from pathlib import Path
import sys
import time
import uuid
import argparse
import httpx
from sqlalchemy import select
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mvp.db import transaction,rows
from mvp.models import Tenant,ModelConfig,Conversation,Usage
from mvp.service import audit
parser=argparse.ArgumentParser();parser.add_argument('--single',action='store_true');args=parser.parse_args()
with transaction() as s:tenant=s.scalar(select(Tenant).where(Tenant.slug=='biaofun'))
with transaction(tenant.id) as s:
    cfg=s.get(ModelConfig,(tenant.id,'primary')).config
    if not cfg.get('enabled'):raise SystemExit('The tested model must already be enabled by the administrator.')
    result={'synthetic':True,'model':cfg.get('model'),'turns':[]}
with httpx.Client(base_url='http://127.0.0.1:8767',trust_env=False,timeout=10) as client:
    response=client.post('/api/public/session/biaofun');response.raise_for_status();auth=response.json()
    client.headers.update({'Authorization':'Bearer '+auth['token'],'X-CSRF-Token':auth['csrf']})
    response=client.post('/api/public/conversations',json={});response.raise_for_status();cid=response.json()['id']
    with transaction(tenant.id) as s:
        s.get(Conversation,(tenant.id,cid)).name='优化联调 · 内部测试'
        audit(s,tenant.id,'local-operator','verification.optimization',cid,{'synthetic':True,'turn_limit':4})
    questions=['你好','我们想做一个企业官网，需要准备哪些资料？','那大概多少钱？','先不做官网了，想做CRM系统，需要准备哪些资料？']
    if args.single:questions=questions[1:2]
    for question in questions:
        started=time.monotonic()
        response=client.post('/api/public/conversations/'+cid+'/messages',json={'text':question,'client_id':'optimization-'+uuid.uuid4().hex});response.raise_for_status()
        message=response.json()['message'];reply=None
        while time.monotonic()-started<55:
            response=client.get('/api/public/conversations/'+cid);response.raise_for_status()
            replies=[m for m in response.json()['messages'] if m['role']=='ai' and m['sequence']>message['sequence']]
            if replies:reply=replies[0];break
            time.sleep(1)
        if not reply:raise SystemExit('Synthetic turn timed out; inspect its durable AI job.')
        with transaction(tenant.id) as s:
            usage=s.scalar(rows(s,Usage,tenant.id).where(Usage.detail['message_id'].as_string()==message['id']).order_by(Usage.created_at.desc()).limit(1))
            diagnostic={'outcome':usage.outcome,'route':usage.detail.get('route'),'latency_ms':usage.latency_ms,'provider':usage.detail.get('provider_diagnostic',{})} if usage else {}
        result['turns'].append({'question':question,'answer':reply['text'],'citations':reply['citations'],'elapsed_ms':round((time.monotonic()-started)*1000),'diagnostic':diagnostic})
result['scope']=str(len(result['turns']))+' non-PII synthetic turns. Usage is linked to each message. This is not the 100-question human business-quality assessment or p95 performance proof.'
(ROOT/('docs/optimization-model-single.json' if args.single else 'docs/optimization-model-live.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
print(json.dumps(result,ensure_ascii=False))
