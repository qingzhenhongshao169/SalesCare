"""Employee-only document ingestion and tenant-scoped retrieval configuration."""
import html
import json
from fastapi import APIRouter,Depends,Request,UploadFile,File,Form,HTTPException
from fastapi.responses import FileResponse,HTMLResponse,JSONResponse
from pydantic import BaseModel,Field,ConfigDict
from .security import principal,require,serialize
from .db import transaction,rows,locked
from .models import *
from .knowledge_documents import upload,document_view,publish,withdraw,preview,storage_path,engine_config
from .knowledge_engine import DEFAULT,embed,rerank
from .service import audit
from server.model_gateway import protect,validate_url,grounded_answer

router=APIRouter()
def auth(request:Request):return principal(request,write=request.method not in ('GET','HEAD','OPTIONS'))
class Input(BaseModel):model_config=ConfigDict(extra='forbid')
class CollectionInput(Input):name:str=Field(min_length=1,max_length=120)

@router.get('/api/kb/collections')
def collections(p=Depends(auth)):
    require(p,'admin','editor','publisher')
    with transaction(p['tenant']) as s:
        from .db import serialization_lock
        serialization_lock(s,p['tenant'],'default-kb')
        existing=list(s.scalars(rows(s,KBCollection,p['tenant'])))
        if not existing:
            item=KBCollection(tenant_id=p['tenant'],name='企业资料');s.add(item);s.flush();existing=[item]
        return [serialize(x) for x in existing]

@router.get('/api/kb/channels')
def visible_channels(p=Depends(auth)):
    require(p,'admin','editor','publisher')
    with transaction(p['tenant']) as s:
        return [{'id':c.id,'name':c.name,'enabled':c.enabled} for c in s.scalars(rows(s,Channel,p['tenant']))]

@router.post('/api/kb/collections')
def create_collection(body:CollectionInput,p=Depends(auth)):
    require(p,'admin','editor')
    with transaction(p['tenant']) as s:
        if s.scalar(rows(s,KBCollection,p['tenant']).where(KBCollection.name==body.name.strip())):raise HTTPException(409,'知识库名称已存在。')
        item=KBCollection(tenant_id=p['tenant'],name=body.name.strip());s.add(item);s.flush();return serialize(item)

@router.get('/api/kb/documents')
def documents(collection_id:str|None=None,p=Depends(auth)):
    require(p,'admin','editor','publisher')
    with transaction(p['tenant']) as s:
        docs=s.scalars(rows(s,KBDocument,p['tenant']).where(KBDocument.deleted==False).order_by(KBDocument.created_at.desc())).all()
        return [document_view(s,p['tenant'],d) for d in docs if not collection_id or d.collection_id==collection_id]

@router.post('/api/kb/documents/upload')
async def file_upload(file:UploadFile=File(...),collection_id:str=Form(...),visibility:str=Form('internal'),
                      document_id:str|None=Form(None),channel_ids:str=Form('[]'),p=Depends(auth)):
    require(p,'admin','editor')
    data=bytearray()
    try:
        while block:=await file.read(65536):
            data.extend(block)
            if len(data)>20_000_000:raise HTTPException(413,'单个文件不能超过20MB。')
        try:channels=json.loads(channel_ids)
        except ValueError:raise HTTPException(422,'渠道格式不正确。') from None
        if not isinstance(channels,list) or len(channels)>20 or any(not isinstance(x,str) for x in channels):raise HTTPException(422,'渠道格式不正确。')
        return upload(p,file.filename or '',bytes(data),collection_id,visibility,document_id,channels)
    finally:await file.close()

@router.get('/api/kb/documents/{did}')
def detail(did:str,p=Depends(auth)):
    require(p,'admin','editor','publisher')
    with transaction(p['tenant']) as s:
        doc=s.get(KBDocument,(p['tenant'],did))
        if not doc or doc.deleted:raise HTTPException(404,'资料不存在。')
        return document_view(s,p['tenant'],doc)

@router.get('/api/kb/versions/{vid}/preview')
def normalized(vid:str,p=Depends(auth)):
    require(p,'admin','editor','publisher')
    with transaction(p['tenant']) as s:
        version=s.get(KBDocumentVersion,(p['tenant'],vid))
        if not version:raise HTTPException(404,'版本不存在。')
        doc=s.get(KBDocument,(p['tenant'],version.document_id))
        if doc.deleted:raise HTTPException(404,'资料已删除。')
        return version.normalized

@router.get('/api/kb/versions/{vid}/standard')
def standard(vid:str,p=Depends(auth)):
    result=normalized(vid,p)
    return JSONResponse(result,headers={'Content-Disposition':'attachment; filename="'+vid+'.knowledge.json"'})

@router.get('/api/kb/versions/{vid}/original')
def original(vid:str,p=Depends(auth)):
    require(p,'admin','editor','publisher')
    with transaction(p['tenant']) as s:
        v=s.get(KBDocumentVersion,(p['tenant'],vid))
        if not v:raise HTTPException(404,'版本不存在。')
        doc=s.get(KBDocument,(p['tenant'],v.document_id))
        if doc.deleted:raise HTTPException(404,'资料不存在。')
        return FileResponse(storage_path(v.storage_key),filename=doc.filename,media_type='application/octet-stream')

@router.post('/api/kb/versions/{vid}/retry')
def retry(vid:str,p=Depends(auth)):
    require(p,'admin','editor')
    with transaction(p['tenant']) as s:
        v=locked(s,KBDocumentVersion,p['tenant'],vid)
        if not v or v.status!='failed':raise HTTPException(409,'仅可重试失败的版本。')
        doc=s.get(KBDocument,(p['tenant'],v.document_id))
        if doc.deleted:raise HTTPException(404,'资料不存在。')
        job=s.scalar(rows(s,KBJob,p['tenant']).where(KBJob.version_id==vid))
        job.status='pending';job.stage='queued';job.progress=0;job.error='';job.lease_until=0;job.attempts=0;v.status='queued'
        audit(s,p['tenant'],p['id'],'document.retry',doc.id)
        return {'ok':True}

class PublishInput(Input):confirm_issues:bool=False
class BatchPublishInput(Input):
    version_ids:list[str]=Field(min_length=1,max_length=50)
    confirm_issues:bool=False

@router.post('/api/kb/publish-batch')
def publish_batch(body:BatchPublishInput,p=Depends(auth)):
    require(p,'admin','publisher')
    results=[]
    for vid in dict.fromkeys(body.version_ids):
        if len(vid)>40:raise HTTPException(422,'版本编号无效。')
        try:results.append({'version_id':vid,**publish(p,vid,body.confirm_issues)})
        except HTTPException as e:results.append({'version_id':vid,'status':'blocked','message':e.detail,'code':e.status_code})
    return {'results':results}

@router.post('/api/kb/versions/{vid}/publish')
def publish_version(vid:str,body:PublishInput,p=Depends(auth)):
    require(p,'admin','publisher');return publish(p,vid,body.confirm_issues)

@router.post('/api/kb/documents/{did}/withdraw')
def withdraw_document(did:str,p=Depends(auth)):
    require(p,'admin','publisher');withdraw(p,did);return {'ok':True}

class ScopeInput(Input):
    visibility:str
    channel_ids:list[str]=Field(default=[],max_length=20)

@router.put('/api/kb/documents/{did}/scope')
def document_scope(did:str,body:ScopeInput,p=Depends(auth)):
    require(p,'admin','publisher')
    if body.visibility not in ('public','internal'):raise HTTPException(422,'请选择有效资料范围。')
    from .db import serialization_lock
    with transaction(p['tenant']) as s:
        serialization_lock(s,p['tenant'],'document:'+did)
        doc=locked(s,KBDocument,p['tenant'],did)
        if not doc or doc.deleted:raise HTTPException(404,'资料不存在。')
        if doc.active_version:raise HTTPException(409,'请先撤回此资料，再调整范围并重新确认发布。')
        for cid in body.channel_ids:
            if not s.get(Channel,(p['tenant'],cid)):raise HTTPException(422,'渠道不属于本企业。')
        doc.visibility=body.visibility;doc.channel_ids=list(dict.fromkeys(body.channel_ids))
        audit(s,p['tenant'],p['id'],'document.scope_changed',did,{'visibility':body.visibility})
    return {'ok':True,'message':'范围已修改，重新确认发布后才会生效。'}

@router.post('/api/kb/documents/{did}/rebuild')
def rebuild(did:str,p=Depends(auth)):
    require(p,'admin','editor')
    with transaction(p['tenant']) as s:
        doc=s.get(KBDocument,(p['tenant'],did))
        if not doc or doc.deleted:raise HTTPException(404,'资料不存在。')
        v=s.scalar(rows(s,KBDocumentVersion,p['tenant']).where(KBDocumentVersion.document_id==did).order_by(KBDocumentVersion.number.desc()).limit(1))
        data=storage_path(v.storage_key).read_bytes();collection=doc.collection_id;filename=doc.filename;visibility=doc.visibility
    return upload(p,filename,data,collection,visibility,did,rebuild=True)

@router.delete('/api/kb/documents/{did}')
def delete_document(did:str,p=Depends(auth)):
    require(p,'admin','publisher')
    from sqlalchemy import delete
    paths=[]
    with transaction(p['tenant']) as s:
        doc=locked(s,KBDocument,p['tenant'],did)
        if not doc or doc.deleted:raise HTTPException(404,'资料不存在。')
        doc.deleted=True;doc.active_version=None
        for c in s.scalars(rows(s,KBChunk,p['tenant']).where(KBChunk.document_id==did)):
            item=s.get(Knowledge,(p['tenant'],c.id))
            if item:item.status='withdrawn';item.content='';item.keywords=[]
        for v in s.scalars(rows(s,KBDocumentVersion,p['tenant']).where(KBDocumentVersion.document_id==did)):
            paths.append(storage_path(v.storage_key));v.status='deleted';v.normalized={};v.issues=[]
            for job in s.scalars(rows(s,KBJob,p['tenant']).where(KBJob.version_id==v.id)):
                job.status='cancelled';job.lease_until=0
        s.execute(delete(KBChunk).where(KBChunk.tenant_id==p['tenant'],KBChunk.document_id==did))
        audit(s,p['tenant'],p['id'],'document.deleted',did)
    # Only individually resolved original files within this document store.
    for path in paths:path.unlink(missing_ok=True)
    return {'ok':True}

class QueryInput(Input):
    query:str=Field(min_length=1,max_length=1200)
    collection_id:str|None=None
    generate:bool=False

@router.post('/api/kb/search')
def search(body:QueryInput,p=Depends(auth)):
    require(p,'admin','editor','publisher')
    with transaction(p['tenant']) as s:
        result=preview(s,p['tenant'],body.query,body.collection_id)
    if body.generate:
        # Reuse the regular AI queue for consumer use; employee draft preview is
        # retrieval-only until a separate metered generation workflow is enabled.
        result['generation_notice']='此页提供检索结果；发布后在企业客户咨询页测试实际模型回答。'
    return result

class EngineInput(Input):
    embedding_mode:str='local'
    embedding_model:str=Field(default=DEFAULT['embedding_model'],max_length=150)
    embedding_url:str=Field(default='',max_length=500)
    embedding_key:str=Field(default='',max_length=4096)
    rerank_mode:str='local'
    rerank_model:str=Field(default=DEFAULT['rerank_model'],max_length=150)
    rerank_url:str=Field(default='',max_length=500)
    rerank_key:str=Field(default='',max_length=4096)
    candidate_limit:int=Field(default=16,ge=4,le=40)
    timeout:int=Field(default=20,ge=5,le=45)

@router.get('/api/kb/engine')
def get_engine(p=Depends(auth)):
    require(p,'admin')
    with transaction(p['tenant']) as s:cfg,keys=engine_config(s,p['tenant'])
    return {**cfg,'embedding_key_saved':bool(keys.get('embedding_key')),'rerank_key_saved':bool(keys.get('rerank_key'))}

@router.put('/api/kb/engine')
def save_engine(body:EngineInput,p=Depends(auth)):
    require(p,'admin')
    if body.embedding_mode not in ('local','api','keyword') or body.rerank_mode not in ('local','api','off'):raise HTTPException(422,'请选择支持的模型模式。')
    if body.embedding_mode=='local' and body.embedding_model not in ('BAAI/bge-small-zh-v1.5','sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'):raise HTTPException(422,'本地Embedding模型不受支持。')
    if body.rerank_mode=='local' and body.rerank_model!=DEFAULT['rerank_model']:raise HTTPException(422,'本地重排模型不受支持。')
    for mode,url in ((body.embedding_mode,body.embedding_url),(body.rerank_mode,body.rerank_url)):
        if mode=='api':validate_url(url)
    with transaction(p['tenant']) as s:
        row=s.get(KBEngineConfig,(p['tenant'],'primary'))
        if not row:row=KBEngineConfig(tenant_id=p['tenant'],id='primary');s.add(row)
        cfg,keys=engine_config(s,p['tenant'])
        for kind in ('embedding','rerank'):
            value=getattr(body,kind+'_key').strip()
            if getattr(body,kind+'_mode')=='api' and getattr(body,kind+'_url')!=cfg.get(kind+'_url') and not value:raise HTTPException(422,'更换服务地址后请填写对应新密钥。')
            if value:keys[kind+'_key']=value
        row.config=body.model_dump(exclude={'embedding_key','rerank_key'});row.cipher=protect(json.dumps(keys)) if keys else ''
        audit(s,p['tenant'],p['id'],'knowledge.engine_saved','primary')
    return {'ok':True,'message':'已保存。更换Embedding模型后，已有资料需要重建候选版本再发布。'}

@router.post('/api/kb/engine/test')
def test_engine(p=Depends(auth)):
    require(p,'admin')
    with transaction(p['tenant']) as s:cfg,keys=engine_config(s,p['tenant'])
    try:
        vectors=embed(['产品说明与售后服务'],cfg,keys)
        _,mode=rerank('售后服务',[{'content':'产品说明包含售后服务。'},{'content':'企业品牌介绍。'}],cfg,keys)
        return {'embedding_dimension':len(vectors[0]),'rerank':mode,'ok':cfg['rerank_mode']=='off' or mode!='fusion_fallback'}
    except Exception:raise HTTPException(502,'模型检测失败，请检查模式、地址、密钥或本地模型资源。') from None

@router.get('/api/public/document-source/{kid}')
def public_source(kid:str,p=Depends(auth)):
    require(p,'visitor')
    with transaction(p['tenant']) as s:
        chunk=s.get(KBChunk,(p['tenant'],kid));item=s.get(Knowledge,(p['tenant'],kid))
        if not chunk or not item or item.status!='published':raise HTTPException(404,'资料已撤回或不存在。')
        doc=s.get(KBDocument,(p['tenant'],chunk.document_id))
        if doc.deleted or doc.visibility!='public' or doc.active_version!=chunk.version_id or (doc.channel_ids and p.get('channel_id') not in doc.channel_ids):raise HTTPException(404,'资料不可访问。')
        if p.get('channel_id'):
            ch=s.get(Channel,(p['tenant'],p['channel_id']))
            if not ch or not ch.enabled or (ch.config.get('knowledge_ids') and kid not in ch.config['knowledge_ids']):raise HTTPException(404,'资料不可访问。')
        return HTMLResponse('<!doctype html><meta charset="utf-8"><title>知识来源</title><main style="max-width:800px;margin:40px auto;font:16px sans-serif;line-height:1.8"><h1>'+html.escape(doc.title)+'</h1><p>'+html.escape(doc.filename)+' · '+html.escape(str(chunk.location))+'</p><pre style="white-space:pre-wrap">'+html.escape(chunk.content)+'</pre></main>')
