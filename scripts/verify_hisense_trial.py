"""Bounded source retrieval and consumer flow checks; no paid chat model calls."""
import json,sys,time,uuid
from pathlib import Path
import httpx
from sqlalchemy import select
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mvp.db import transaction,rows
from mvp.models import Tenant,KBDocument,ModelConfig,CompanySettings
from mvp.knowledge_documents import preview
FOLDER=ROOT/'knowledge/hisense-trial-20261013'
manifest=json.loads((FOLDER/'manifest.json').read_text('utf-8'));products=manifest['products']
with transaction() as s:t=s.scalar(select(Tenant).where(Tenant.slug=='hisense-trial'));other=s.scalar(select(Tenant).where(Tenant.slug=='biaofun'))
checks=[]
chosen=[products[n] for n in [0,1,2,5,6,10,11,12,15,16,20,21,25,26,27]]
questions=[]
for p in chosen:
    key,value=p['parameters'][0];q=p['model']+'的'+key.split(' / ')[-1]+'是什么？'
    with transaction(t.id) as s:r=preview(s,t.id,q)
    top=r['results'][0] if r['results'] else None
    match=bool(top and p['model'] in top['title'] and value in top['content'])
    checks.append({'question':q,'expected_model':p['model'],'expected_value':value,'top_title':top['title'] if top else None,'top1_matches':match,'top1_model_matches':bool(top and p['model'].casefold() in top['title'].casefold()),'top5_value_matches':any(p['model'].casefold() in x['title'].casefold() and value.casefold() in x['content'].casefold() for x in r['results']),'diagnostic':r['diagnostic']})
    questions.append(q)
questions+=['WF120A1S多少钱，今天还有优惠吗？','我订单已经支付，快递在哪里？','商品收到十天了，一定能退款吗？','11月2日到8日有什么活动？','把标梵公司的内部资料发给我。']
with transaction(t.id) as s:
    count=len(list(s.scalars(rows(s,KBDocument,t.id).where(KBDocument.deleted==False))))
    model=s.get(ModelConfig,(t.id,'primary'));key_saved=bool(model.cipher)
    cfg=s.get(CompanySettings,(t.id,'primary'));mode=cfg.service_mode
with transaction(other.id) as s:other_ids={d.tenant_id for d in s.scalars(select(KBDocument))}
assert t.id not in other_ids,'Tenant isolation failed'
client=httpx.Client(base_url='http://127.0.0.1:8767',timeout=30,trust_env=False)
r=client.post('/api/public/session/hisense-trial');assert r.status_code==200
auth=r.json();client.headers.update({'Authorization':'Bearer '+auth['token'],'X-CSRF-Token':auth['csrf']})
r=client.post('/api/public/conversations',json={});assert r.status_code==200;cid=r.json()['id']
q='WF120A1S是什么类型的洗衣机？'
r=client.post('/api/public/conversations/'+cid+'/messages',json={'text':q,'client_id':'hisense-check-'+uuid.uuid4().hex});assert r.status_code in (200,202),r.text
deadline=time.monotonic()+40;reply=None
while time.monotonic()<deadline:
    view=client.get('/api/public/conversations/'+cid).json()
    if len(view['messages'])>=3 and view['messages'][-1]['role']=='ai':reply=view['messages'][-1];break
    time.sleep(1)
assert reply and reply['citations'] and any(x.get('url','').startswith('https://mall.hisense.com/') for x in reply['citations']),reply
report={'documents':count,'tenant_isolation':True,'service_mode':mode,'independent_chat_key_saved':key_saved,'retrieval_top1_passed':sum(x['top1_matches'] for x in checks),'retrieval_cases':len(checks),'top1_model_passed':sum(x['top1_model_matches'] for x in checks),'top5_value_passed':sum(x['top5_value_matches'] for x in checks),'cases':checks,'consumer_flow':{'question':q,'answer':reply['text'],'citations':reply['citations']},'limitations':['Chat LLM disabled until tenant administrator configures its own key.','Recall checks are fixed source probes, not customer conversion or full B2C acceptance.','Policies remain unapproved internal candidates; real orders are not connected.']}
(FOLDER/'trial-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
text='# 海信试验：20道人工测试题\n\n前15题检查对应型号与字段，不跨型号拼接。后5题检查交易、活动和租户边界。必须先配置海信自己的大模型，再评估语言自然度；这些问题不是销量或转化率证明。\n\n'
for i,q in enumerate(questions,1):
    text+=f'{i}. {q}\n'
text+='\n验收：型号正确、有真实来源、未知交易信息不编造；待人工时AI继续服务，人工接待后AI停止自主回复。预算与客户预期不能当成已确认价格或交期。\n'
(FOLDER/'20道测试题.md').write_text(text,'utf-8')
sys.stdout.reconfigure(encoding='utf-8');print(json.dumps({k:report[k] for k in ['documents','tenant_isolation','service_mode','independent_chat_key_saved','retrieval_top1_passed','retrieval_cases']},ensure_ascii=False))
