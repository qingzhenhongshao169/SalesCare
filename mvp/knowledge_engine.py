"""Real local/API embeddings, hybrid RRF and bounded reranking. No tenant secrets shared."""
import hashlib
import json
import math
import re
import threading
from functools import lru_cache
import httpx
from server.model_gateway import protect,validate_url
from server.retrieval import retrieve,tokens
from .config import RUNTIME

DEFAULT={'embedding_mode':'local','embedding_model':'BAAI/bge-small-zh-v1.5',
         'embedding_url':'','rerank_mode':'local','rerank_model':'BAAI/bge-reranker-v2-m3-int8',
         'rerank_url':'','candidate_limit':16,'timeout':20}
LOCK=threading.RLock()

class EngineError(Exception):pass

def fingerprint(cfg):
    data={k:cfg.get(k,DEFAULT.get(k)) for k in ('embedding_mode','embedding_model','embedding_url')}
    return hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()

@lru_cache(maxsize=2)
def local_embedding(name):
    from fastembed import TextEmbedding
    if name not in ('BAAI/bge-small-zh-v1.5','sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'):
        raise EngineError('local_model')
    return TextEmbedding(model_name=name,cache_dir=str(RUNTIME/'models'),threads=2)

@lru_cache(maxsize=1)
def local_reranker(name):
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    if name!='BAAI/bge-reranker-v2-m3-int8':raise EngineError('local_model')
    return TextCrossEncoder(model_name=name,cache_dir=str(RUNTIME/'models'),threads=2)

def api_call(url,key,payload,timeout):
    try:
        validate_url(url,resolve=True)
        with httpx.Client(timeout=timeout,trust_env=False,follow_redirects=False) as client:
            response=client.post(url,headers={'Authorization':'Bearer '+key},json=payload)
            if response.status_code!=200:raise EngineError('provider_'+str(response.status_code))
            return response.json()
    except EngineError:raise
    except Exception:raise EngineError('provider_connection') from None

def embed(texts,cfg,secrets=None,query=False):
    cfg={**DEFAULT,**cfg}
    if cfg['embedding_mode']=='keyword':return [[] for _ in texts]
    if cfg['embedding_mode']=='local':
        try:
            with LOCK:
                model=local_embedding(cfg['embedding_model'])
                # Reject oversize inputs explicitly rather than silently embed a prefix.
                from tokenizers import Tokenizer
                tokenizer=Tokenizer.from_str(model.model.tokenizer.to_str())
                tokenizer.no_truncation();tokenizer.no_padding()
                if any(len(tokenizer.encode(t).ids)>512 for t in texts):
                    raise EngineError('embedding_input_limit')
                result=list(model.query_embed(texts) if query else model.embed(texts,batch_size=16))
            vectors=[r.tolist() for r in result]
        except EngineError:raise
        except Exception:raise EngineError('local_model_failed') from None
    else:
        key=(secrets or {}).get('embedding_key','')
        if not key:raise EngineError('embedding_key_missing')
        data=api_call(cfg['embedding_url'].rstrip('/')+'/embeddings',key,
                      {'model':cfg['embedding_model'],'input':texts},cfg['timeout'])
        try:vectors=[d['embedding'] for d in sorted(data['data'],key=lambda d:d['index'])]
        except Exception:raise EngineError('provider_format') from None
    if len(vectors)!=len(texts):raise EngineError('vector_count')
    size=len(vectors[0]) if vectors else 0
    if not 8<=size<=8192 or any(len(v)!=size or any(not isinstance(x,(int,float)) or not math.isfinite(x) for x in v) for v in vectors):
        raise EngineError('vector_format')
    return vectors

def cosine(a,b):
    if len(a)!=len(b) or not a:return -1
    divisor=math.sqrt(sum(x*x for x in a)*sum(x*x for x in b))
    return sum(x*y for x,y in zip(a,b))/divisor if divisor else -1

def scope_by_model(query,records):
    """Explicit model identifiers are exact constraints, not semantic similarities."""
    pattern=r'(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9/_.+-]*\d[A-Za-z0-9/_.+-]*|(?<![A-Za-z0-9])\d+[A-Za-z][A-Za-z0-9/_.+-]*'
    ignored=r'(?:\d+(?:\.\d+)?(?:w|kw|hz|khz|kg|gb|tb|cm|mm|m|db|v|a|g|k)|wi-?fi\d|hdmi[\d.]+|usb[\d.]+|v[\d.]+|b2b|b2c|rgb|mini-?led)'
    models=list(dict.fromkeys(x.rstrip('.').lower() for x in re.findall(pattern,query) if not re.fullmatch(ignored,x,re.I)))
    if not models:return records,{}
    def matches(record,model):
        return bool(re.search(r'(?<![A-Za-z0-9])'+re.escape(model)+r'(?![A-Za-z0-9/_.+-])',record['title']+' '+record['content'],re.I))
    filtered=[r for r in records if any(matches(r,m) for m in models)]
    missing=[m for m in models if not any(matches(r,m) for r in filtered)]
    return filtered,{'model_scope':'exact' if filtered else 'unavailable','requested_models':models,'missing_models':missing}

def rerank(query,records,cfg,secrets):
    if not records:return [],'none'
    cfg={**DEFAULT,**cfg}
    if cfg['rerank_mode']=='off':return records,'fusion'
    try:
        if cfg['rerank_mode']=='local':
            with LOCK:scores=list(local_reranker(cfg['rerank_model']).rerank(query,[r['content'] for r in records],batch_size=4))
            ordered=sorted(zip(scores,records),key=lambda x:float(x[0]),reverse=True)
            return [r for score,r in ordered if float(score)>-6.0],'local_model'
        key=secrets.get('rerank_key','')
        if not key:raise EngineError('rerank_key_missing')
        data=api_call(cfg['rerank_url'].rstrip('/')+'/rerank',key,
                      {'model':cfg['rerank_model'],'query':query,'documents':[r['content'] for r in records],'top_n':len(records)},cfg['timeout'])
        result=data.get('results',data.get('data',[]));indices=[x['index'] for x in result]
        if len(set(indices))!=len(records) or set(indices)!=set(range(len(records))):raise EngineError('rerank_format')
        return [records[i] for i in indices],'api_model'
    except Exception:return records,'fusion_fallback'

def hybrid(query,records,vector_map,cfg,secrets,limit=4):
    cfg={**DEFAULT,**cfg};capacity=cfg['candidate_limit']
    if re.search(r'天气|下雨|气温|彩票|股票代码',query):return [],{'embedding':'out_of_scope','rerank':'none'}
    records,model_scope=scope_by_model(query,records)
    if not records:return [],{'embedding':'not_needed','rerank':'none',**model_scope}
    lexical=retrieve({'knowledge':records},'web',query,__import__('time').time(),limit=capacity)
    semantic=[];diagnostic={'embedding':'keyword','rerank':'none',**model_scope}
    if vector_map and cfg['embedding_mode']!='keyword':
        try:
            q=embed([query],cfg,secrets,query=True)[0]
            scored=[(cosine(q,vector_map[r['id']]),r) for r in records if r['id'] in vector_map]
            # Semantic similarity is a ranking signal, not confidence in truth.
            semantic=[r for score,r in sorted(scored,key=lambda x:x[0],reverse=True)[:capacity] if score>.12]
            diagnostic['embedding']='vector'
        except EngineError:diagnostic['embedding']='keyword_fallback'
    scores={};by_id={r['id']:r for r in records}
    for ranking in (lexical,semantic):
        for n,r in enumerate(ranking,1):scores[r['id']]=scores.get(r['id'],0)+1/(60+n)
    candidates=[by_id[k] for k in sorted(scores,key=scores.get,reverse=True)[:capacity]]
    ranked,diagnostic['rerank']=rerank(query,candidates,cfg,secrets)
    return ranked[:limit],diagnostic
