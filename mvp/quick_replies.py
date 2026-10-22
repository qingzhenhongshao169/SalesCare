"""Exact reviewed FAQs; no semantic answer cache or cross-customer personalization."""
import hashlib
import json
import re
import time
from sqlalchemy import select
from .models import Knowledge
from .knowledge_retrieval import PostgresScan
from .knowledge_provenance import digest


def question_key(text):
    return re.sub(r'[\s，。！？、,.!?：:；;“”"\'（）()]+','',text).lower()


def signature(value):
    return digest(json.dumps(value,sort_keys=True,ensure_ascii=False))


def lookup(session,tid,channel_id,question,history):
    # A short follow-up may depend on previous budgets, products or negations.
    # Only a self-contained exact question qualifies; no fuzzy similarity match.
    if re.search(r'刚才|之前|改成|改为|那(?:个|款|么|你|就)|这个|这款|上面|前面',question):return None
    key=question_key(question)
    if not key:return None
    candidates=[]
    for row in session.execute(select(Knowledge.id,Knowledge.quick_reply).where(
            Knowledge.tenant_id==tid,Knowledge.status=='published',Knowledge.visibility=='public',
            Knowledge.quick_reply['enabled'].as_boolean()==True)):
        config=row.quick_reply or {}
        if key in {question_key(q) for q in config.get('questions',[])}:candidates.append((row.id,config))
    if not candidates:return None
    configs=dict(candidates);valid=[]
    for record,_,_ in PostgresScan(session,tid,channel_id).scan(ids=list(configs)):
        config=configs[record['id']]
        if config.get('source_digest')!=digest(record['content']) or config.get('source_version')!=record['version']:continue
        if not config.get('reviewer_id') or not config.get('answer'):continue
        valid.append({'answer':config['answer'],'records':[record],'signature':signature(config),'knowledge_id':record['id']})
    # Ambiguous authorized FAQ keys never pick one arbitrarily.
    return valid[0] if len(valid)==1 else None


def still_current(session,tid,reply):
    row=session.get(Knowledge,(tid,reply['knowledge_id']),populate_existing=True)
    return bool(row and signature(row.quick_reply)==reply['signature'] and row.quick_reply.get('enabled'))
