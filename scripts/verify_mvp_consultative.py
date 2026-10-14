"""Three synthetic live turns, scoped usage evidence, no credentials printed."""
import json
from pathlib import Path
import sys
import time
import uuid
import httpx
from sqlalchemy import select
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mvp.db import transaction,rows
from mvp.models import Tenant,Conversation,Usage
from mvp.service import audit

with transaction() as s:tenant=s.scalar(select(Tenant).where(Tenant.slug=='biaofun'))
result={'date':'2026-10-12','synthetic':True,'turns':[],
        'scope':'Three synthetic live turns with configured model; not business scoring or proof of conversion uplift.'}
channel='e7583dfb6c5a4fdab959c01afd0d7d32'
with httpx.Client(base_url='http://127.0.0.1:8767',trust_env=False,timeout=10) as client:
    response=client.post('/api/public/session/biaofun',params={'channel':channel});response.raise_for_status();identity=response.json()
    client.headers.update({'Authorization':'Bearer '+identity['token'],'X-CSRF-Token':identity['csrf']})
    response=client.post('/api/public/conversations',json={});response.raise_for_status();cid=response.json()['id']
    with transaction(tenant.id) as s:
        s.get(Conversation,(tenant.id,cid)).name='需求分析修复 · 内部合成验收'
        audit(s,tenant.id,'local-operator','verification.consultative',cid,{'synthetic':True})
    for question in ['网站建设500页面多少钱',
                     '网站建设，没有网站，核心功能展示公司，上线时间希望尽快2周，预算1万',
                     '是500条产品内容，不是500个独立页面，资料都有了，下一步怎么推进？']:
        at=time.monotonic()
        response=client.post('/api/public/conversations/'+cid+'/messages',json={'text':question,'client_id':'consultative-'+uuid.uuid4().hex});response.raise_for_status()
        sequence=response.json()['message']['sequence'];message_id=response.json()['message']['id'];reply=None
        while time.monotonic()-at<50:
            response=client.get('/api/public/conversations/'+cid);response.raise_for_status()
            matches=[m for m in response.json()['messages'] if m['role']=='ai' and m['sequence']>sequence]
            if matches:reply=matches[0];break
            time.sleep(1)
        if not reply:raise SystemExit('Synthetic reply did not arrive; inspect worker before retrying.')
        with transaction(tenant.id) as s:
            uses=s.scalars(rows(s,Usage,tenant.id).order_by(Usage.created_at.desc()).limit(40)).all()
            use=next(u for u in uses if u.detail.get('conversation_id')==cid and u.detail.get('message_id')==message_id)
        result['turns'].append({'question':question,'answer':reply['text'],'citations':reply['citations'],
                                'outcome':use.outcome,'kind':use.kind,'latency_ms':use.latency_ms})
result['all_live_model']=all(t['outcome']=='llm' for t in result['turns'])
result['no_repeated_reply']=len({t['answer'] for t in result['turns']})==len(result['turns'])
(ROOT/'docs/consultative-live-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
print(json.dumps(result,ensure_ascii=False))
