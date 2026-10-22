"""Reconcile only budget/timeline from a tenant's matching visitor conversation.

Dry run by default. No model calls, message edits or company commitments.
"""
import argparse
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from mvp.db import transaction,rows,locked
from mvp.models import Tenant,Conversation,Message
from mvp.service import audit
from server.requirements import update_profile_requirements

parser=argparse.ArgumentParser()
parser.add_argument('--tenant',required=True)
parser.add_argument('--contains',required=True)
parser.add_argument('--hours',type=int,default=24)
parser.add_argument('--apply',action='store_true')
args=parser.parse_args()
if not args.contains.strip() or not 1<=args.hours<=168:raise SystemExit('Use a specific text and 1–168 hour window')
with transaction() as session:tid=session.scalar(select(Tenant.id).where(Tenant.slug==args.tenant))
if not tid:raise SystemExit('Tenant not found')
with transaction(tid) as session:
    ids=list(session.scalars(rows(session,Message,tid).where(Message.role=='visitor',
        Message.created_at>=time.time()-args.hours*3600,Message.text.contains(args.contains,autoescape=True))
        .with_only_columns(Message.conversation_id).distinct()))
changed=0
for cid in ids:
    with transaction(tid) as session:
        conversation=locked(session,Conversation,tid,cid)
        if not conversation or conversation.state=='CLOSED':continue
        profile=dict(conversation.profile)
        for message in session.scalars(rows(session,Message,tid).where(Message.conversation_id==cid,Message.role=='visitor').order_by(Message.sequence)):
            profile=update_profile_requirements(profile,message.text)
        if profile==conversation.profile:continue
        changed+=1
        if args.apply:
            conversation.profile=profile
            audit(session,tid,'system','profile.requirements_reconciled',cid,{'fields':['budget','timeline'],'sequence':conversation.sequence})
print({'matched_conversations':len(ids),'changed_conversations':changed,'applied':args.apply})
