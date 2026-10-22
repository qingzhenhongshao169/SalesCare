"""Bind exact FAQ questions to unchanged, already-reviewed public source text.

No generated facts and no new human approval is fabricated. Edited sources, high-risk
sources, documents and existing administrator-configured replies are never replaced.
"""
import argparse
import json
import sys
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from sqlalchemy import select
from mvp.db import transaction,rows
from mvp.models import Tenant,Knowledge,KBChunk
from mvp.service import audit
from mvp.knowledge_provenance import digest

parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
package=json.loads((ROOT/'knowledge/biaofun.json').read_text('utf-8'))
selected={'bf_001','bf_002','bf_004','bf_005','bf_006','bf_008','bf_009','bf_010','bf_011','bf_012'}
aliases={'bf_001':['你们是什么公司？','介绍一下公司。'],
         'bf_002':['怎么联系你们？'],
         'bf_005':['你们主要做什么？','你们主要提供哪些服务？','你们有哪些业务？']}
with transaction() as s:tid=s.scalar(select(Tenant.id).where(Tenant.slug=='biaofun'))
if not tid:raise SystemExit('Target enterprise not provisioned')
results=[]
with transaction(tid) as s:
    for source in package['entries']:
        if source['id'] not in selected:continue
        k=s.scalar(rows(s,Knowledge,tid).where(Knowledge.id==source['id']).with_for_update())
        if not k or k.status!='published' or k.visibility!='public' or k.risk=='high' or not k.reviewer_id:continue
        if k.content!=source['content'] or len(k.content)>1000 or k.quick_reply or s.get(KBChunk,(tid,k.id)):continue
        questions=list(dict.fromkeys([k.title,*source.get('questions',[]),*aliases.get(k.id,[])]))[:20]
        if args.apply:
            k.quick_reply={'enabled':True,'questions':questions,'answer':k.content,'source_digest':digest(k.content),
                           'source_version':k.version,'reviewer_id':k.reviewer_id,'reviewed_at':time.time(),
                           'approval_basis':'unchanged_previously_published_source','answer_mode':'source_verbatim'}
            audit(s,tid,'source-verbatim','quick_reply.bound_to_reviewed_source',k.id,{'version':k.version})
        results.append({'knowledge_id':k.id,'source_version':k.version,'question_count':len(questions)})
print(json.dumps({'applied':args.apply,'unchanged_reviewed_sources':results},ensure_ascii=False))
