"""Bounded live-model sampling through the real public API, no credentials output.

Creates marked internal synthetic consultations, never leads or reception tasks.
Question 17/18 wording is sampled only; state transitions are covered separately
by isolated database tests. Outputs are evidence for human review, not a score.
"""
import json
from pathlib import Path
import sys
import time
import uuid
import argparse
import httpx
from sqlalchemy import select

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.stdout.reconfigure(encoding='utf-8')
from mvp.db import transaction,rows
from mvp.models import Tenant,ModelConfig,Conversation,Usage
from mvp.service import audit
from server.dialogue_policy import POLICY_VERSION

IDS=['Q001','Q010','Q011','Q012','Q014','Q017','Q021','Q022','Q031','Q032',
     'Q033','Q039','Q041','Q043','Q049','Q050','Q052','Q053','Q066','Q068']
parser=argparse.ArgumentParser()
parser.add_argument('--numbers',help='Comma-separated selected question numbers; omitting runs all 20.')
args=parser.parse_args()
selected=sorted(set(int(n) for n in args.numbers.split(','))) if args.numbers else list(range(1,21))
if any(n not in range(1,21) for n in selected):raise SystemExit('Question numbers must be 1..20.')
for followup,prior in ((10,9),(14,13),(18,17)):
    if followup in selected and prior not in selected:raise SystemExit('A follow-up needs its prior question.')
bank={q['id']:q for q in json.loads((ROOT/'mvp/evaluation/business-questions.json').read_text('utf-8'))}
with transaction() as s:tenant=s.scalar(select(Tenant).where(Tenant.slug=='biaofun'))
if not tenant:raise SystemExit('Configured company not found.')
with transaction(tenant.id) as s:
    if not s.get(ModelConfig,(tenant.id,'primary')).config.get('enabled'):
        raise SystemExit('Administrator must already have enabled the model.')

result={'date':'2026-10-13','policy':POLICY_VERSION,'synthetic':True,'turns':[],
        'scope':'Selected questions via public API. No leads or reception tasks. Human g/s/Y re-scoring is pending; not conversion evidence.'}
target=ROOT/('docs/feedback-answers-targeted-20261013.json' if args.numbers else 'docs/feedback-answers-live-20261013.json')
channel='e7583dfb6c5a4fdab959c01afd0d7d32'
with httpx.Client(base_url='http://127.0.0.1:8767',trust_env=False,timeout=10) as client:
    response=client.post('/api/public/session/biaofun',params={'channel':channel});response.raise_for_status()
    identity=response.json()
    client.headers.update({'Authorization':'Bearer '+identity['token'],'X-CSRF-Token':identity['csrf']})
    consultation_count=0
    for number in selected:
        qid=IDS[number-1]
        # Retain context only for intentional adjacent multi-turn pairs.
        # Two explicit synthetic visitor cohorts, each under the ten-consultation
        # per-hour cap. Never alter rate limits or rotate identities on 429.
        if number not in (10,14,18) and consultation_count==10:
            response=client.post('/api/public/session/biaofun',params={'channel':channel});response.raise_for_status()
            identity=response.json()
            client.headers.update({'Authorization':'Bearer '+identity['token'],'X-CSRF-Token':identity['csrf']})
            consultation_count=0
        if number not in (10,14,18):
            consultation_count+=1
            response=client.post('/api/public/conversations',json={});response.raise_for_status();cid=response.json()['id']
            with transaction(tenant.id) as s:
                s.get(Conversation,(tenant.id,cid)).name=f'回答优化验收 · 内部测试 {number}'
                audit(s,tenant.id,'local-operator','verification.feedback_answers',cid,{'synthetic':True,'question':qid})
        question=bank[qid]['question'];started=time.monotonic()
        response=client.post('/api/public/conversations/'+cid+'/messages',json={'text':question,'client_id':'feedback-'+uuid.uuid4().hex})
        response.raise_for_status();message=response.json()['message'];reply=None
        while time.monotonic()-started<55:
            response=client.get('/api/public/conversations/'+cid);response.raise_for_status()
            replies=[m for m in response.json()['messages'] if m['role']=='ai' and m['sequence']>message['sequence']]
            if replies:reply=replies[0];break
            time.sleep(1)
        if not reply:raise SystemExit(f'Question {number} did not complete; inspect durable job, do not blindly resend.')
        with transaction(tenant.id) as s:
            use=s.scalar(rows(s,Usage,tenant.id).where(Usage.detail['message_id'].as_string()==message['id']).limit(1))
        turn={'number':number,'id':qid,'question':question,'answer':reply['text'],'citations':reply['citations'],
              'outcome':use.outcome,'scene':use.detail.get('scene'),'latency_ms':use.latency_ms,'human_review':'pending'}
        result['turns'].append(turn)
        target.write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
        print(json.dumps({'number':number,'outcome':use.outcome,'answer':reply['text']},ensure_ascii=False),flush=True)
result['completed']=len(result['turns'])==len(selected)
target.write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
print(f'{len(selected)}-question response evidence saved; human review still required.',flush=True)
