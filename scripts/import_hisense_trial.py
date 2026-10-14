"""Import the reviewed official corpus into one independent trial tenant."""
import json,sys,time,html
from pathlib import Path
from sqlalchemy import select
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mvp.db import transaction,rows
from mvp.models import *
from mvp.knowledge_documents import upload,publish
from mvp.service import risk,audit
from mvp.config import settings
FOLDER=ROOT/'knowledge/hisense-trial-20261013'
manifest=json.loads((FOLDER/'manifest.json').read_text('utf-8'))
sys.stdout.reconfigure(encoding='utf-8')
with transaction() as s:t=s.scalar(select(Tenant).where(Tenant.slug=='hisense-trial'))
if not t:raise SystemExit('Create the tenant through the local platform onboarding page first.')
p={'tenant':t.id,'id':'official-trial-curation','kind':'employee','roles':['admin','editor','publisher']}
with transaction(t.id) as s:
    col=s.scalar(rows(s,KBCollection,t.id).where(KBCollection.name=='海信官方公开资料 · 试验'))
    if not col:col=KBCollection(tenant_id=t.id,name='海信官方公开资料 · 试验');s.add(col);s.flush()
    collection=col.id
    cfg=s.get(CompanySettings,(t.id,'primary'));cfg.service_mode='general_support';cfg.required_skill='产品与售后咨询';cfg.source_hosts=['mall.hisense.com','www.hisense.com']
    if not s.get(KBEngineConfig,(t.id,'primary')):s.add(KBEngineConfig(tenant_id=t.id,id='primary',config={'embedding_mode':'local','rerank_mode':'local'}))
    # The chat ModelConfig and its cipher are never changed or copied here.
records=[]
for item in manifest['documents']:
    f=FOLDER/'知识文档'/item['file'];data=f.read_bytes()
    r=upload(p,f.name,data,collection,visibility=item['visibility'])
    if '--rebuild' in sys.argv and r['duplicate']:
        r=upload(p,f.name,data,collection,visibility=item['visibility'],document_id=r['document_id'],rebuild=True)
    records.append({**item,**r})
deadline=time.monotonic()+360
while time.monotonic()<deadline:
    with transaction(t.id) as s:states={r['version_id']:s.get(KBDocumentVersion,(t.id,r['version_id'])).status for r in records}
    pending=sum(x in ('queued','processing') for x in states.values())
    print('Files awaiting construction:',pending,flush=True)
    if not pending:break
    time.sleep(10)
result=[]
for item in records:
    with transaction(t.id) as s:
        v=s.get(KBDocumentVersion,(t.id,item['version_id']));status=v.status
        high=risk(' '.join(b['text'] for b in v.normalized.get('blocks',[])))=='high'
    if item['kind']!='policy' and not high and status=='ready':status=publish(p,item['version_id'],True)['status']
    result.append({'file':item['file'],'kind':item['kind'],'document_id':item['document_id'],'version_id':item['version_id'],'status':status,'review_required':item['kind']=='policy' or high})
with transaction(t.id) as s:
    if any(r['status']=='published' for r in result):s.get(CompanySettings,(t.id,'primary')).published=True
    model=s.get(ModelConfig,(t.id,'primary'));key_saved=bool(model.cipher)
    audit(s,t.id,p['id'],'trial.corpus_imported',collection,{'count':len(result)})
report={'tenant':t.slug,'count':len(result),'published':sum(x['status']=='published' for x in result),'ready':sum(x['status']=='ready' for x in result),'chat_key_saved':key_saved,'documents':result}
(FOLDER/'import-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
invite=ROOT/'.mvp-runtime/hisense-admin-invite.json'
if invite.exists():
    url=json.loads(invite.read_text('utf-8'))['invite_url']
    (ROOT/'.mvp-runtime/海信管理员开通.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>海信试验管理员开通</title><style>body{max-width:720px;margin:60px auto;font:17px sans-serif;line-height:1.8;color:#22322e}a{color:#0b806d}</style><h1>海信试验空间（非官方）</h1><p>账号：admin1；姓名：海信管理员。点击下方设置自己的密码。邀请从创建起24小时有效，仅可使用一次。</p><a href="'+html.escape(url,quote=True)+'">设置管理员密码并开通</a><p>大模型密钥之后在本企业后台的“模型连接”填写，标梵配置没有复制过来。</p></html>','utf-8')
print('Imported:',report['count'],'published:',report['published'],'candidates:',report['ready'],'independent chat key saved:',key_saved)
