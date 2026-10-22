"""Isolated QA services. Never reads or calls a real tenant's model credentials."""
import asyncio
import json
import os
import sys
import time
import re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
runtime=ROOT/'.mvp-runtime'
cfg=json.loads((runtime/'test-config.json').read_text('utf-8'))
cfg.update(mode='lan-pilot',public_origin='http://127.0.0.1:8768',cookie_suffix='_speed_qa')
path=runtime/'speed-qa-config.json';path.write_text(json.dumps(cfg),'utf-8')
os.environ['SALESCARE_MVP_CONFIG']=str(path)
os.environ['SALESCARE_ALLOW_LOCAL_LLM']='1'
slug='response-speed-qa'

if sys.argv[1]=='provider':
    import uvicorn
    from fastapi import FastAPI,Request
    from fastapi.responses import JSONResponse
    fake=FastAPI()
    @fake.post('/v1/chat/completions')
    async def complete(request:Request):
        body=await request.json();await asyncio.sleep(.9)
        text=body.get('messages',[{}])[-1].get('content','')
        answer=('您可以先整理公司介绍和产品服务资料，再结合要展示的内容确认栏目。'
                if '预算' not in text else '预算和展示需求已记下，接下来可以整理希望展示的栏目，具体范围再由顾问核算。')
        system='\n'.join(m.get('content','') for m in body.get('messages',[]) if m.get('role')=='system')
        ids=list(dict.fromkeys(re.findall(r'"id"\s*:\s*"([^"]+)"',system)))
        return JSONResponse({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'answer':answer,'knowledge_ids':ids[:1]},ensure_ascii=False)}}]})
    uvicorn.run(fake,host='127.0.0.1',port=8772,log_level='error',access_log=False)
elif sys.argv[1]=='prepare':
    from sqlalchemy import select
    from mvp.db import transaction
    from mvp.models import Tenant,CompanySettings,ModelConfig,Knowledge
    from mvp.knowledge_provenance import digest
    from server.model_gateway import protect
    with transaction() as s:
        t=s.scalar(select(Tenant).where(Tenant.slug==slug))
        if not t:t=Tenant(slug=slug,name='响应速度 · 合成验收');s.add(t);s.flush()
        tid=t.id
    with transaction(tid) as s:
        if not s.get(CompanySettings,(tid,'primary')):s.add(CompanySettings(tenant_id=tid,id='primary',published=True,weekdays=list(range(7)),opens='00:00',closes='23:59',daily_ai_limit=5000,daily_token_limit=10_000_000))
        model=s.get(ModelConfig,(tid,'primary'))
        if not model:model=ModelConfig(tenant_id=tid,id='primary');s.add(model)
        model.config={'protocol':'openai','base_url':'http://127.0.0.1:8772/v1','model':'synthetic-latency','enabled':True,'timeout':5,'max_output_tokens':800}
        model.cipher=protect('synthetic-qa-only-not-a-real-api-key')
        k=s.get(Knowledge,(tid,'faq'))
        if not k:k=Knowledge(tenant_id=tid,id='faq',family='faq',title='企业官网需要准备什么资料？',content='企业官网可以先提供公司介绍、产品服务资料、参考网站和希望上线的时间。',keywords=['官网','网站','建设','准备','资料'],status='published',author_id='synthetic-editor',reviewer_id='synthetic-publisher');s.add(k);s.flush()
        k.quick_reply={'enabled':True,'questions':['企业官网需要准备什么资料？'],'answer':k.content,'source_digest':digest(k.content),'source_version':k.version,'reviewer_id':'synthetic-publisher'}
    print('Synthetic QA tenant ready; model is local delayed mock only.')
elif sys.argv[1]=='api':
    import uvicorn
    uvicorn.run('mvp.app:app',host='127.0.0.1',port=8768,access_log=False,log_level='warning')
elif sys.argv[1]=='worker':
    from sqlalchemy import select
    from mvp.db import transaction
    from mvp.models import Tenant
    from mvp import worker
    with transaction() as s:tid=s.scalar(select(Tenant.id).where(Tenant.slug==slug))
    if not tid:raise RuntimeError('Run prepare first')
    worker.tenant_ids=lambda:[tid]  # Never lease other synthetic tests' pending jobs.
    worker.main()
else:raise SystemExit('Use prepare, api, worker or provider')
