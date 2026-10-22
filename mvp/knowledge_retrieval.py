"""Tenant-authorized, bounded-memory retrieval over the existing PostgreSQL store.

PostgresScan is the adapter boundary: a future external index must return candidates,
then pass revalidate(). This implementation is exact O(N), NOT an ANN index.
"""
import heapq
import time
from sqlalchemy import select, and_, or_, literal
from server.retrieval import LexicalScorer
from .db import transaction
from .models import Knowledge, KBChunk, KBDocument, KBDocumentVersion, Channel
from .knowledge_engine import embed, cosine, rerank, fingerprint, scope_by_model, EngineError
from .knowledge_documents import engine_config
from .knowledge_provenance import digest, vector_digest

BATCH_SIZE = 128


class PostgresScan:
    def __init__(self, session, tenant, channel=None):
        self.session, self.tenant, self.channel = session, tenant, channel

    def scan(self, vectors=False, ids=None):
        tid, s = self.tenant, self.session
        ch = s.get(Channel, (tid, self.channel), populate_existing=True) if self.channel else None
        if self.channel and (not ch or not ch.enabled):
            return
        k, c, d, v = Knowledge, KBChunk, KBDocument, KBDocumentVersion
        query = select(
            k.id, k.title, k.content, k.keywords, k.version, k.category, k.source_url,
            c.id.label('chunk_id'), c.document_id, c.version_id, c.digest.label('chunk_digest'),
            c.content.label('canonical_content'), c.location, c.vector_digest,
            (c.vector if vectors else literal(None)).label('vector'),
            v.number, v.index_fingerprint, v.build_manifest, v.digest.label('source_digest'),
        ).outerjoin(c, and_(c.tenant_id == k.tenant_id, c.id == k.id))
        query = query.outerjoin(d, and_(d.tenant_id == c.tenant_id, d.id == c.document_id))
        query = query.outerjoin(v, and_(v.tenant_id == c.tenant_id, v.id == c.version_id))
        query = query.where(k.tenant_id == tid, k.status == 'published', k.visibility == 'public',
            or_(k.expires_at.is_(None), k.expires_at > time.time()),
            or_(and_(c.id.is_(None), k.category != '文档知识'),
                and_(d.deleted == False, d.visibility == 'public', d.active_version == c.version_id,
                     v.status == 'published', v.document_id == d.id)))
        # JSON scope is checked per streamed row; no cross-tenant unrestricted scan.
        query = query.add_columns(d.channel_ids)
        if ch and ch.config.get('knowledge_ids'):
            query = query.where(k.id.in_(ch.config['knowledge_ids']))
        if ids is not None:
            query = query.where(k.id.in_(ids))
        result = s.execute(query.order_by(k.created_at, k.id).execution_options(yield_per=BATCH_SIZE))
        try:
            for r in result.mappings():
                if r['channel_ids'] and self.channel not in r['channel_ids']:
                    continue
                content_hash = digest(r['content'])
                if r['chunk_id'] and (r['canonical_content'] != r['content'] or r['chunk_digest'] != content_hash):
                    continue
                manifest = r['build_manifest'] or {}
                if manifest and (manifest.get('tenant_id') != tid or manifest.get('version_id') != r['version_id']
                                 or manifest.get('source_digest') != r['source_digest']
                                 or manifest.get('embedding_fingerprint') != r['index_fingerprint']):
                    continue
                evidence = {'tenant_id': tid, 'content_digest': content_hash}
                record = {key: r[key] for key in ('id', 'title', 'content', 'keywords', 'category', 'version', 'source_url')}
                record.update(status='published', visibility='public', channels=['web'])
                if r['chunk_id']:
                    evidence.update(document_id=r['document_id'], version_id=r['version_id'],
                                    source_digest=r['source_digest'], generation=manifest.get('generation', 'legacy'),
                                    index_fingerprint=r['index_fingerprint'])
                    record.update(document_id=r['document_id'], version=r['number'], source_location=r['location'],
                                  source_url='/api/public/document-source/'+r['id'])
                record['evidence'] = evidence
                vector = r['vector'] or []
                if vector and ((r['vector_digest'] and vector_digest(vector) != r['vector_digest'])
                               or (manifest and len(vector) != manifest.get('dimension'))):
                    vector = []
                yield record, vector, r['index_fingerprint']
        finally:
            result.close()


def revalidate(session, tid, records, channel_id=None, lock=False):
    """Use canonical DB records, not data supplied by the model or a search backend."""
    if not records:
        return []
    expected = {r['id']: r for r in records}
    if lock:
        # Same order as lifecycle mutations: channel -> documents -> knowledge.
        # Hold read locks until reply commit so revocation cannot slip between
        # final authorization and message insertion.
        if channel_id:
            session.execute(select(Channel.id).where(Channel.tenant_id==tid,Channel.id==channel_id).with_for_update(read=True)).all()
        documents = sorted({r['document_id'] for r in records if r.get('document_id')})
        if documents:
            session.execute(select(KBDocument.id).where(KBDocument.tenant_id==tid,KBDocument.id.in_(documents)).order_by(KBDocument.id).with_for_update(read=True)).all()
        session.execute(select(Knowledge.id).where(Knowledge.tenant_id==tid,Knowledge.id.in_(expected)).order_by(Knowledge.id).with_for_update(read=True)).all()
    fresh = {}
    for r, _, _ in PostgresScan(session, tid, channel_id).scan(ids=list(expected)):
        old = expected[r['id']]
        if (old.get('evidence') == r['evidence'] and old['version'] == r['version']
                and old['content'] == r['content'] and old['title'] == r['title']
                and old.get('source_url') == r.get('source_url')):
            fresh[r['id']] = r
    return [fresh[r['id']] for r in records if r['id'] in fresh]


def validate_citations(session, tid, references, channel_id=None):
    """Canonical document-aware validation for delayed channel delivery.

    Older messages have no evidence snapshot; immutable document chunks still
    require their active document version, scope and canonical content. New
    messages additionally bind the complete generation/content identity.
    """
    if not references:return True
    if any(not isinstance(ref,dict) or not isinstance(ref.get('id'),str) for ref in references):return False
    current={r['id']:r for r,_,_ in PostgresScan(session,tid,channel_id).scan(ids=[x['id'] for x in references])}
    for ref in references:
        record=current.get(ref['id'])
        if not record or record['version']!=ref.get('version'):return False
        if ref.get('evidence') is not None and record['evidence']!=ref['evidence']:return False
        if ref.get('title') is not None and ref['title']!=record['title']:return False
        if ref.get('url') is not None and ref['url']!=record['source_url']:return False
    return True


def _keep(heap, score, sequence, record, capacity):
    item = (score, -sequence, record)
    if len(heap) < capacity:
        heapq.heappush(heap, item)
    elif item[:2] > heap[0][:2]:
        heapq.heapreplace(heap, item)


def search(tid, channel_id, query, limit=4, checkpoint=None, remaining=None):
    """At most candidate_limit per ranker and one embedding/rerank request per run."""
    import re
    if re.search(r'天气|下雨|气温|足球|彩票|股票代码', query):
        return [], {'embedding': 'out_of_scope', 'rerank': 'none', 'rerank_reason':'out_of_scope', 'backend': 'postgres_scan'}
    check = checkpoint or (lambda: None)
    with transaction(tid) as s:
        cfg, keys = engine_config(s, tid)
        capacity = max(4, min(40, int(cfg['candidate_limit'])))
        fp = fingerprint(cfg)
        provider = PostgresScan(s, tid, channel_id)
        scorer = LexicalScorer(query)
        _, scope = scope_by_model(query, [])
        requested = scope.get('requested_models', [])
        found = set()
        vector_available = False
        stale_vectors = 0
        for i, (r, _, index_fp) in enumerate(provider.scan()):
            if i % BATCH_SIZE == 0:
                check()
            selected, matches = scope_by_model(query, [r])
            if not selected:
                continue
            found.update(set(requested) - set(matches.get('missing_models', [])))
            scorer.observe(r)
            vector_available |= index_fp == fp and cfg['embedding_mode'] != 'keyword'
            stale_vectors += bool(index_fp and index_fp != fp)
    diagnostic = {'backend': 'postgres_scan', 'scanned': scorer.count, 'candidate_limit': capacity,
                  'embedding': 'keyword', 'rerank': 'none', 'stale_index_chunks': stale_vectors}
    if requested:
        diagnostic.update(model_scope='exact' if found else 'unavailable', requested_models=requested,
                          missing_models=[m for m in requested if m not in found])
    if not scorer.count:
        diagnostic['rerank_reason']='no_candidates'
        return [], diagnostic
    q = None
    check()
    if vector_available:
        try:
            call_cfg = {**cfg, 'timeout': min(cfg['timeout'], remaining())} if remaining else cfg
            q = embed([query], call_cfg, keys, query=True)[0]
            diagnostic['embedding'] = 'vector'
        except EngineError:
            diagnostic['embedding'] = 'keyword_fallback'
    check()
    lexical, semantic = [], []
    mismatches = 0
    with transaction(tid) as s:
        for i, (r, vector, index_fp) in enumerate(PostgresScan(s, tid, channel_id).scan(vectors=q is not None)):
            if i % BATCH_SIZE == 0:
                check()
            if not scope_by_model(query, [r])[0]:
                continue
            score = scorer.score(r)
            if score is not None:
                _keep(lexical, score, i, r, capacity)
            if q is not None and vector and index_fp == fp:
                if len(q) != len(vector):
                    mismatches += 1
                elif (score := cosine(q, vector)) > .12:
                    _keep(semantic, score, i, r, capacity)
    scores, by_id = {}, {}
    for heap in (lexical, semantic):
        for rank, (_, _, r) in enumerate(sorted(heap, key=lambda x: x[:2], reverse=True), 1):
            scores[r['id']] = scores.get(r['id'], 0) + 1/(60+rank)
            by_id[r['id']] = r
    candidates = [by_id[key] for key in sorted(scores, key=scores.get, reverse=True)[:capacity]]
    diagnostic.update(candidates=len(candidates), vector_dimension_mismatches=mismatches,
                      max_retained_candidates=2*capacity, batch_size=BATCH_SIZE)
    with transaction(tid) as s:
        candidates = revalidate(s, tid, candidates, channel_id)
    check()
    call_cfg = {**cfg, 'timeout': min(cfg['timeout'], remaining())} if remaining else cfg
    if cfg.get('rerank_mode','off')=='off':
        ranked=candidates;diagnostic.update(rerank='fusion',rerank_reason='disabled')
    elif not candidates:
        ranked=[];diagnostic.update(rerank='none',rerank_reason='no_candidates')
    elif call_cfg['timeout']<=.05:
        ranked=candidates;diagnostic.update(rerank='fusion',rerank_reason='insufficient_time')
    else:
        ranked,diagnostic['rerank']=rerank(query,candidates,call_cfg,keys)
        diagnostic['rerank_reason']='provider_fallback' if diagnostic['rerank']=='fusion_fallback' else 'executed'
    check()
    return ranked[:limit], diagnostic
