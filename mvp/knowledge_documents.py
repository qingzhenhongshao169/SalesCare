"""Tenant document lifecycle, source-preserving publication and retrieval integration."""
import hashlib
import json
import re
from pathlib import Path
from sqlalchemy import select,delete
from fastapi import HTTPException
from .config import RUNTIME
from .db import transaction,rows,locked,serialization_lock
from .models import *
from .security import serialize
from .service import audit,risk,redacted_text
from .document_parser import FORMATS,normalize,chunks,ParseError
from .knowledge_engine import DEFAULT,fingerprint,embed,hybrid,EngineError
from .knowledge_provenance import build_manifest,vector_digest
from server.model_gateway import protect

ERRORS={'unsupported':'暂不支持此格式，请使用PDF、DOCX、XLSX、CSV、PPTX、TXT或Markdown。',
        'ocr_required':'文件或页面没有可读取文字，请上传文字版；本机尚未配置OCR。',
        'encrypted':'文件已加密，请上传可读取版本。','archive_limit':'文件解压后的内容超过安全限制。',
        'content_limit':'正文超过200万字符限制，请按业务主题分成多个文件。',
        'table_limit':'表格超过行列处理限制，请按工作表或业务范围拆分。',
        'page_limit':'PDF超过300页，请拆分后上传。','empty':'未读取到有效文字。',
        'encoding':'无法识别文字编码。','chunk_limit':'分块数量超过限制，请拆分文件。',
        'local_model_failed':'本地模型未能运行，请检测知识检索配置。',
        'embedding_input_limit':'部分标题或表格行过长，超过本地模型输入限制，请拆分；原件和标准化内容已保留。',
        'embedding_key_missing':'请在知识检索配置中填写Embedding密钥。','failed':'处理失败，请检查文件后重试。',
        'config_changed':'构建期间检索配置变化，请按新配置重试。',
        'source_integrity':'原件摘要不一致，请核对后重新上传。',
        'chunk_integrity':'分块内容校验不一致，请重建此资料。',
        'vector_integrity':'向量校验不一致，请重建此资料。',
        'vector_dimension_changed':'模型返回的向量维度发生变化，请核对配置后重建。',
        'lease_exhausted':'处理任务多次中断，已停止自动重试。请检查进程和文件后手动重试。',
        'provider_connection':'模型连接暂时失败，请稍后重试。',
        'provider_429':'模型服务限流，请稍后重试。'}

def chunk_keywords(text):
    return list(dict.fromkeys(re.findall(r'(?<![A-Za-z0-9_-])[A-Z0-9][A-Z0-9_-]{2,}(?![A-Za-z0-9_-])',text)))[:30]

def engine_config(session,tid):
    row=session.get(KBEngineConfig,(tid,'primary'))
    return ({**DEFAULT,**(row.config if row else {})},json.loads(protect(row.cipher,decrypt=True)) if row and row.cipher else {})

def storage_path(key):
    root=(RUNTIME/'documents').resolve();path=(root/key).resolve()
    if root not in path.parents:raise HTTPException(404,'文件不存在。')
    return path

def upload(p,filename,data,collection_id,visibility='internal',document_id=None,channel_ids=None,rebuild=False):
    filename=Path(filename.replace('\\','/')).name[:250]
    if Path(filename).suffix.lower() not in FORMATS:raise HTTPException(422,ERRORS['unsupported'])
    if not data or len(data)>20_000_000:raise HTTPException(413,'单个文件应在20MB以内，且不能为空。')
    if visibility not in ('public','internal'):raise HTTPException(422,'请选择可见范围。')
    tid=p['tenant'];sha=hashlib.sha256(data).hexdigest()
    with transaction(tid) as s:
        serialization_lock(s,tid,'document-upload')
        collection=s.get(KBCollection,(tid,collection_id))
        if not collection:raise HTTPException(404,'知识库不存在。')
        for cid in channel_ids or []:
            if not s.get(Channel,(tid,cid)):raise HTTPException(422,'渠道不属于本企业。')
        doc=s.get(KBDocument,(tid,document_id)) if document_id else None
        if document_id and (not doc or doc.deleted):raise HTTPException(404,'资料不存在。')
        if not doc:
            existing=s.scalar(rows(s,KBDocumentVersion,tid).join(KBDocument,(KBDocument.tenant_id==KBDocumentVersion.tenant_id)&(KBDocument.id==KBDocumentVersion.document_id)).where(KBDocumentVersion.digest==sha,KBDocument.collection_id==collection_id,KBDocument.deleted==False).limit(1))
            if existing:return {'document_id':existing.document_id,'version_id':existing.id,'duplicate':True}
            doc=KBDocument(tenant_id=tid,collection_id=collection_id,filename=filename,title=Path(filename).stem,visibility=visibility,channel_ids=channel_ids or [],author_id=p['id'])
            s.add(doc);s.flush()
        previous=s.scalar(rows(s,KBDocumentVersion,tid).where(KBDocumentVersion.document_id==doc.id).order_by(KBDocumentVersion.number.desc()).limit(1))
        if previous and previous.digest==sha and not rebuild:return {'document_id':doc.id,'version_id':previous.id,'duplicate':True}
        vid=uid();key=tid+'/'+doc.id+'/'+vid+Path(filename).suffix.lower()
        path=storage_path(key);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        version=KBDocumentVersion(tenant_id=tid,id=vid,document_id=doc.id,number=previous.number+1 if previous else 1,digest=sha,storage_key=key)
        s.add(version);s.flush();s.add(KBJob(tenant_id=tid,version_id=vid))
        audit(s,tid,p['id'],'document.uploaded',doc.id,{'version':version.number,'filename':filename})
        return {'document_id':doc.id,'version_id':vid,'duplicate':False}

def document_view(s,tid,doc):
    versions=list(s.scalars(rows(s,KBDocumentVersion,tid).where(KBDocumentVersion.document_id==doc.id).order_by(KBDocumentVersion.number.desc())))
    result=serialize(doc);result['versions']=[]
    for v in versions:
        job=s.scalar(rows(s,KBJob,tid).where(KBJob.version_id==v.id).order_by(KBJob.created_at.desc()).limit(1))
        result['versions'].append({'id':v.id,'number':v.number,'status':v.status,'issues':v.issues,
            'blocks':len(v.normalized.get('blocks',[])),'characters':v.normalized.get('characters',0),
            'build':v.build_manifest or {},
            'chunk_count':s.scalar(select(__import__('sqlalchemy').func.count()).select_from(KBChunk).where(KBChunk.tenant_id==tid,KBChunk.version_id==v.id)),
            'job':{'status':job.status,'stage':job.stage,'progress':job.progress,'attempts':job.attempts,
                   'max_attempts':3,'retry_at':job.lease_until if job.status=='pending' and job.error else None,
                   'error':ERRORS.get(job.error,'模型或文件处理失败，请检测配置。') if job.error else ''} if job else None})
    return result

def process(tid,jid,lease_token=None):
    with transaction(tid) as s:
        job=locked(s,KBJob,tid,jid)
        if not job or job.status!='leased' or job.lease_until<=__import__('time').time() or (lease_token is not None and job.lease_token!=lease_token):return
        version=s.get(KBDocumentVersion,(tid,job.version_id));doc=s.get(KBDocument,(tid,version.document_id))
        if doc.deleted:job.status='cancelled';return
        cfg,keys=engine_config(s,tid);fp=fingerprint(cfg);path=storage_path(version.storage_key);filename=doc.filename
        job.stage='parsing';job.progress=10;version.status='processing';token=job.lease_token;source_digest=version.digest
    try:
        if hashlib.sha256(path.read_bytes()).hexdigest()!=source_digest:raise ParseError('source_integrity')
        normalized=normalize(path,filename);pieces=chunks(normalized,max_chars=360)
        with transaction(tid) as s:
            job=locked(s,KBJob,tid,jid)
            if job.status!='leased' or job.lease_token!=token or job.lease_until<=__import__('time').time():return
            job.stage='embedding';job.progress=35
            version=s.get(KBDocumentVersion,(tid,job.version_id));version.normalized=normalized;version.issues=normalized['issues']
        vectors=[]
        for start in range(0,len(pieces),16):
            vectors.extend(embed([x['content'] for x in pieces[start:start+16]],cfg,keys))
            with transaction(tid) as s:
                job=locked(s,KBJob,tid,jid)
                if job.status!='leased' or job.lease_token!=token or job.lease_until<=__import__('time').time():return
                job.progress=35+int(55*min(start+16,len(pieces))/len(pieces));job.lease_until=__import__('time').time()+300
        with transaction(tid) as s:
            job=locked(s,KBJob,tid,jid);v=s.get(KBDocumentVersion,(tid,job.version_id));doc=s.get(KBDocument,(tid,v.document_id))
            if job.status!='leased' or job.lease_token!=token or job.lease_until<=__import__('time').time() or doc.deleted:return
            current,_=engine_config(s,tid)
            if fingerprint(current)!=fp:raise EngineError('config_changed')
            s.execute(delete(KBChunk).where(KBChunk.tenant_id==tid,KBChunk.version_id==v.id))
            built=[]
            if len(vectors)!=len(pieces):raise EngineError('vector_count')
            for n,(piece,vector) in enumerate(zip(pieces,vectors)):
                c=KBChunk(tenant_id=tid,document_id=doc.id,version_id=v.id,position=n,title=piece['title'],content=piece['content'],location={**piece['location'],'block_ids':piece['block_ids']},digest=piece['digest'],vector=vector,vector_digest=vector_digest(vector))
                s.add(c);built.append(c)
            try:v.build_manifest=build_manifest(tid,v,built,fp,cfg['embedding_mode'],normalized['parser_version'])
            except ValueError as exc:raise EngineError(str(exc)) from None
            doc.title=normalized['title'];v.index_fingerprint=fp;v.status='ready';job.status='done';job.stage='ready';job.progress=100;job.lease_until=0
            audit(s,tid,'knowledge-worker','document.ready',doc.id,{'version':v.number,'chunks':len(pieces)})
    except Exception as exc:
        code=str(exc) if isinstance(exc,(ParseError,EngineError)) else 'failed'
        with transaction(tid) as s:
            job=locked(s,KBJob,tid,jid)
            if job.lease_token!=token or job.status!='leased' or job.lease_until<=__import__('time').time():return
            retryable=code in ('provider_connection','provider_429','provider_500','provider_502','provider_503','provider_504')
            retry=retryable and job.attempts<3
            job.status='pending' if retry else 'failed';job.stage='retry_wait' if retry else 'failed'
            job.error=code if re.fullmatch(r'[a-z_0-9]{1,60}',code) else 'failed'
            job.lease_until=__import__('time').time()+min(120,5*2**job.attempts) if retry else 0
            job.lease_token=''
            s.get(KBDocumentVersion,(tid,job.version_id)).status='queued' if retry else 'failed'
            audit(s,tid,'knowledge-worker','document.retry_scheduled' if retry else 'document.failed',job.version_id,{'reason':job.error,'attempts':job.attempts})

def publish(p,vid,confirm_issues=False):
    tid=p['tenant']
    with transaction(tid) as s:
        v=locked(s,KBDocumentVersion,tid,vid)
        if not v:raise HTTPException(404,'版本不存在。')
        serialization_lock(s,tid,'document:'+v.document_id);doc=locked(s,KBDocument,tid,v.document_id)
        if doc.deleted or v.status not in ('ready','superseded'):raise HTTPException(409,'此版本尚未构建完成，或已经发布。')
        if v.issues and not confirm_issues:raise HTTPException(409,'存在解析提示，请核对并确认后发布。')
        pieces=list(s.scalars(rows(s,KBChunk,tid).where(KBChunk.version_id==vid).order_by(KBChunk.position)))
        if not pieces:raise HTTPException(409,'此版本没有可发布分块。')
        cfg,_=engine_config(s,tid)
        if v.index_fingerprint!=fingerprint(cfg):raise HTTPException(409,'Embedding配置已变化，请重建候选版本后发布。')
        try:
            checked=build_manifest(tid,v,pieces,v.index_fingerprint,cfg['embedding_mode'],v.normalized.get('parser_version','native-v1'))
        except ValueError:raise HTTPException(409,'知识构建校验失败，请重建此版本后发布。') from None
        if v.build_manifest and checked!=v.build_manifest:raise HTTPException(409,'构建清单与当前分块不一致，请重建候选版本。')
        # Legacy versions acquire a manifest only after explicit publication checks.
        v.build_manifest=checked
        # Minimal conflict signal for the same named document; use an explicit
        # replacement version instead of silently publishing two contradictory copies.
        date_pattern=r'\d{1,2}月\d{1,2}日\s*[至到—-]\s*(?:\d{1,2}月)?\d{1,2}日'
        dates=set(re.findall(date_pattern,' '.join(c.content for c in pieces)))
        for other in s.scalars(rows(s,KBDocument,tid).where(KBDocument.id!=doc.id,KBDocument.title==doc.title,KBDocument.active_version.is_not(None),KBDocument.deleted==False)):
            if doc.channel_ids and other.channel_ids and not set(doc.channel_ids).intersection(other.channel_ids):continue
            old=s.get(KBDocumentVersion,(tid,other.active_version))
            old_dates=set(re.findall(date_pattern,' '.join(b['text'] for b in old.normalized.get('blocks',[]))))
            if dates and old_dates and dates!=old_dates:raise HTTPException(409,'同名已发布资料的日期不同，请核对范围；需要替代时请在原资料上传新版本。')
        if risk(doc.title+' '.join(x.content for x in pieces))=='high':
            if doc.author_id==p['id']:raise HTTPException(403,'高风险资料需另一位发布者审核。')
            if not s.get(Member,(tid,doc.author_id)):
                approvals=list(v.approvals or [])
                if p['id'] not in {a['actor'] for a in approvals}:approvals.append({'actor':p['id'],'at':__import__('time').time()})
                v.approvals=approvals
                if len(approvals)<2:return {'status':'review_required','message':'已记录审核，请另一位发布者完成复核。'}
        oldids=list(s.scalars(rows(s,KBChunk,tid).where(KBChunk.document_id==doc.id)))
        for old in oldids:
            item=s.get(Knowledge,(tid,old.id))
            if item and item.status=='published':item.status='superseded'
        if doc.active_version:
            old=s.get(KBDocumentVersion,(tid,doc.active_version))
            if old:old.status='superseded'
        for c in pieces:
            item=s.get(Knowledge,(tid,c.id))
            if not item:
                item=Knowledge(tenant_id=tid,id=c.id,family=c.id,title=c.title,content=c.content,keywords=chunk_keywords(c.content),category='文档知识',author_id=doc.author_id)
                s.add(item)
            item.status='published';item.visibility=doc.visibility;item.reviewer_id=p['id']
        doc.active_version=v.id;v.status='published';v.reviewer_id=p['id']
        audit(s,tid,p['id'],'document.published',doc.id,{'version':v.number,'visibility':doc.visibility})
        return {'status':'published','document_id':doc.id,'version_id':v.id}

def withdraw(p,did):
    with transaction(p['tenant']) as s:
        doc=locked(s,KBDocument,p['tenant'],did)
        if not doc:raise HTTPException(404,'资料不存在。')
        for c in s.scalars(rows(s,KBChunk,p['tenant']).where(KBChunk.document_id==did)):
            item=s.get(Knowledge,(p['tenant'],c.id))
            if item:item.status='withdrawn'
        if doc.active_version:s.get(KBDocumentVersion,(p['tenant'],doc.active_version)).status='superseded'
        doc.active_version=None;audit(s,p['tenant'],p['id'],'document.withdrawn',did)

def allowed_documents(s,tid,records,channel_id=None):
    """Filter bridged chunks before either lexical or vector candidate selection."""
    chunks_by={c.id:c for c in s.scalars(rows(s,KBChunk,tid))}
    docs={d.id:d for d in s.scalars(rows(s,KBDocument,tid))}
    output=[];vectors={};cfg,keys=engine_config(s,tid);fp=fingerprint(cfg)
    for record in records:
        chunk=chunks_by.get(record['id'])
        if not chunk:output.append(record);continue
        doc=docs.get(chunk.document_id)
        if not doc or doc.deleted or doc.visibility!='public' or doc.active_version!=chunk.version_id:continue
        if doc.channel_ids and channel_id not in doc.channel_ids:continue
        v=s.get(KBDocumentVersion,(tid,chunk.version_id))
        if v.status!='published':continue
        source=re.search(r'(?:来源|Source|官网来源)[：:]\s*(https://[^\s<>]+)', '\n'.join(b['text'] for b in v.normalized.get('blocks',[])))
        record={**record,'source_url':source[1] if source else '/api/public/document-source/'+chunk.id,'source_location':chunk.location,'document_id':doc.id}
        output.append(record)
        if v.index_fingerprint==fp and chunk.vector:vectors[chunk.id]=chunk.vector
    return output,vectors,cfg,keys

def preview(s,tid,query,collection_id=None):
    records=[];vectors={};cfg,keys=engine_config(s,tid);fp=fingerprint(cfg)
    for doc in s.scalars(rows(s,KBDocument,tid).where(KBDocument.deleted==False)):
        if collection_id and doc.collection_id!=collection_id:continue
        v=s.scalar(rows(s,KBDocumentVersion,tid).where(KBDocumentVersion.document_id==doc.id,KBDocumentVersion.status.in_(['ready','published'])).order_by(KBDocumentVersion.number.desc()).limit(1))
        if not v:continue
        for c in s.scalars(rows(s,KBChunk,tid).where(KBChunk.version_id==v.id)):
            records.append({'id':c.id,'title':c.title,'content':c.content,'keywords':chunk_keywords(c.content),'status':'published','visibility':'public','channels':['web'],'version':v.number,'source_location':c.location,'filename':doc.filename})
            if c.vector and v.index_fingerprint==fp:vectors[c.id]=c.vector
    result,diagnostic=hybrid(query,records,vectors,cfg,keys,limit=5)
    return {'results':result,'diagnostic':diagnostic,'message':'此处为资料检索试问，不代表已发布或模型生成回答。'}
