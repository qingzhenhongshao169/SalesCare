"""Loopback browser QA on the disposable DB. Model is an explicit 900ms local mock."""
import asyncio
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
runtime=ROOT/'.mvp-runtime'
state_path=runtime/'case-browser-qa.json'
action=sys.argv[1]
if action=='prepare':
    run=json.loads((runtime/'case-verification-run.json').read_text('utf-8'))
    assert re.fullmatch(r'salescare_case_test_[a-f0-9]{12}',run['database'])
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    cfg=json.loads(Path(run['config']).read_text('utf-8'))
    cfg.update(mode='lan-pilot',public_origin=f'http://127.0.0.1:{port}',cookie_suffix='_case_browser')
    config_path=runtime/'case-browser-config.json';config_path.write_text(json.dumps(cfg),encoding='utf-8')
    state={'port':port,'slug':'case-qa-'+uuid.uuid4().hex[:8],'config':str(config_path),
           'username':'case-qa-admin','password':'synthetic-browser-qa-only-20261019'}
    os.environ['SALESCARE_MVP_CONFIG']=str(config_path)
else:
    state=json.loads(state_path.read_text('utf-8'))
    os.environ['SALESCARE_MVP_CONFIG']=state['config']
os.environ['SALESCARE_ALLOW_LOCAL_LLM']='1'

if action=='prepare':
    from mvp.db import transaction
    from mvp.models import Tenant,CompanySettings,ModelConfig,Knowledge,Member,Offer,uid
    from mvp.security import hash_password
    from mvp.knowledge_provenance import digest
    from server.model_gateway import protect
    with transaction() as s:
        t=Tenant(slug=state['slug'],name='案例推荐自测 · 合成企业');s.add(t);s.flush();tid=t.id
    state['tenant_id']=tid
    with transaction(tid) as s:
        s.add(CompanySettings(tenant_id=tid,id='primary',published=True,weekdays=list(range(7)),opens='00:00',closes='23:59',
            daily_ai_limit=1000,daily_token_limit=10000000))
        s.add(ModelConfig(tenant_id=tid,id='primary',config={'protocol':'openai','base_url':f'http://127.0.0.1:{state["port"]}/v1',
            'enabled':True,'model':'synthetic-900ms-case-qa','timeout':5,'max_output_tokens':500},cipher=protect('synthetic-local-provider-only')))
        s.add(Member(tenant_id=tid,username=state['username'],name='自测管理员',password_hash=hash_password(state['password']),
            roles=['admin','agent','editor','publisher'],skills=['数字营销与开发咨询'],available=True,heartbeat_at=time.time()))
        faq='可以先整理企业介绍、产品服务资料、品牌素材和参考网站，再按本期目标确定内容。'
        s.add(Knowledge(tenant_id=tid,id='qa-faq',family='qa-faq',title='官网资料准备',content=faq,keywords=['官网','网站','资料'],
            status='published',author_id='synthetic-editor',reviewer_id='synthetic-publisher',
            quick_reply={'enabled':True,'questions':['官网要准备什么资料？'],'answer':faq,'source_digest':digest(faq),
                         'source_version':1,'reviewer_id':'synthetic-publisher'}))
        package=json.loads((ROOT/'knowledge/biaofun-case-studies-20261019.json').read_text('utf-8'))
        for entry in package['entries']:
            meta={**entry['recommendation'],'enabled':True,'source_digest':digest(entry['content']),'source_version':1,
                  'source_url':entry['source_url'],'reviewer_id':'synthetic-qa-review-not-enterprise-approval','reviewed_at':time.time()}
            s.add(Knowledge(tenant_id=tid,id=entry['id'],family=entry['id'],title=entry['title'],content=entry['content'],
                source_url=entry['source_url'],keywords=['官网','案例'],status='published',category='项目案例',
                author_id='synthetic-editor',reviewer_id='synthetic-publisher',case_recommendation=meta))
        s.add(Offer(tenant_id=tid,name='网站建设（自测服务）',description='按需求梳理网站内容。',tags=['官网','网站','外贸'],knowledge_id='qa-faq'))
        s.add(Offer(tenant_id=tid,name='小程序开发（自测服务）',description='按需求评估小程序。',tags=['小程序','会员'],knowledge_id='biaofun-case-27'))
    state_path.write_text(json.dumps(state),encoding='utf-8')
    print(json.dumps({'qa_ready':True,'port':state['port'],'slug':state['slug'],'model':'local synthetic 900ms only'}))
elif action=='api':
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from mvp.app import app
    import uvicorn
    @app.post('/v1/chat/completions')
    async def mock_provider(request:Request):
        body=await request.json();await asyncio.sleep(.9)
        system='\n'.join(m.get('content','') for m in body.get('messages',[]) if m.get('role')=='system')
        ids=list(dict.fromkeys(re.findall(r'"id"\s*:\s*"([^"]+)"',system)))
        user=body.get('messages',[{}])[-1].get('content','')
        answer=('可以先按设备品类、用途和应用场景整理展示内容，首期优先介绍重点设备。' if '设备' in user else
                '可以先整理企业介绍和产品服务资料，再结合本期目标规划官网内容。')
        return JSONResponse({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'answer':answer,'knowledge_ids':ids[:1]},ensure_ascii=False)}}],
                             'usage':{'prompt_tokens':100,'completion_tokens':40}})
    uvicorn.run(app,host='127.0.0.1',port=state['port'],access_log=False,log_level='warning')
elif action=='worker':
    from mvp.worker import lease_ai,run_ai
    while True:
        job=lease_ai(state['tenant_id'])
        if job:run_ai(state['tenant_id'],job)
        else:time.sleep(.02)
