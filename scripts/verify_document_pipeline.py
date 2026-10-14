"""Real local model verification in a synthetic, isolated test database."""
import os,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
os.environ['SALESCARE_MVP_CONFIG']=str(ROOT/'.mvp-runtime/ui-config.json')
from fastapi.testclient import TestClient
from sqlalchemy import select
from mvp.app import app
from mvp.db import transaction,rows
from mvp.models import Tenant,KBChunk,KBEngineConfig,CompanySettings
from mvp.knowledge_documents import process
from mvp.knowledge_worker import lease
with transaction() as s:t=s.scalar(select(Tenant).where(Tenant.slug=='ui-verification'))
with transaction(t.id) as s:
    row=s.get(KBEngineConfig,(t.id,'primary'))
    if row:row.config={'embedding_mode':'local','rerank_mode':'local'}
    s.get(CompanySettings,(t.id,'primary')).service_mode='general_support'
c=TestClient(app,base_url='http://127.0.0.1:8768')
r=c.post('/api/login',json={'tenant':t.slug,'username':'qa-admin','password':'synthetic-ui-test-password'});assert r.status_code==200
c.headers['X-CSRF-Token']=r.json()['csrf']
col=c.get('/api/kb/collections').json()[0]['id']
content='# 验收产品ZX900\n\nZX900电视支持Wi-Fi无线网络连接。打开设置中的网络选项，选择家庭Wi-Fi并输入密码。\n\n# 范围\n\n仅用于程序功能验收。'
r=c.post('/api/kb/documents/upload',data={'collection_id':col,'visibility':'public'},files={'file':('验收产品.md',content.encode())});assert r.status_code==200
d=r.json()
if d['duplicate']:
    from mvp.knowledge_documents import upload
    d=upload({'tenant':t.id,'id':'qa-check'},'normalized-check.md',content.encode(),col,'public',d['document_id'],rebuild=True)
if not d['duplicate']:process(t.id,lease(t.id))
detail=c.get('/api/kb/documents/'+d['document_id']).json();assert detail['versions'][0]['status'] in ('ready','published'),detail
if detail['versions'][0]['status']=='ready':assert c.post('/api/kb/versions/'+d['version_id']+'/publish',json={}).status_code==200
r=c.post('/api/kb/search',json={'query':'ZX900怎么连家里的无线网？'}).json()
assert r['results'] and 'Wi-Fi' in r['results'][0]['content'],r
assert r['diagnostic']['embedding']=='vector' and r['diagnostic']['rerank']=='local_model',r
with transaction(t.id) as s:dimensions=[len(x.vector) for x in s.scalars(rows(s,KBChunk,t.id).where(KBChunk.document_id==d['document_id']))]
assert all(x==512 for x in dimensions)
result={'passed':True,'dimension':512,'chunks':len(dimensions),'retrieval':r['diagnostic'],'scope':'Synthetic tenant; real CPU embedding and reranking, no tenant chat keys used.'}
(ROOT/'docs/document-pipeline-model-check.json').write_text(json.dumps(result,indent=2),'utf-8');print(json.dumps(result))
