import json
from sqlalchemy import select
from .db import transaction
from .models import Tenant, CompanySettings, Knowledge, Offer, ModelConfig
from .config import ROOT


def seed_company():
    with transaction() as session:
        tenant = session.scalar(select(Tenant).where(Tenant.slug=='biaofun'))
        if tenant: return tenant.id
        tenant = Tenant(slug='biaofun',name='标梵互动')
        session.add(tenant);session.flush();tid=tenant.id
    package = json.loads((ROOT/'knowledge/biaofun.json').read_text('utf-8'))
    with transaction(tid) as session:
        session.add(CompanySettings(tenant_id=tid,id='primary',published=False))
        session.add(ModelConfig(tenant_id=tid,id='primary',config={}))
        mapping = {}
        for entry in package['entries']:
            row = Knowledge(tenant_id=tid,id=entry['id'],family=entry.get('family',entry['id']),
                title=entry['title'],content=entry['content'],keywords=entry.get('keywords',[]),category=entry.get('category','企业知识'),
                status=entry['status'],risk=entry.get('risk','low'),version=entry.get('version',1),
                source_url=entry.get('source_url',''),author_id='website-import',reviewer_id='curated-import' if entry['status']=='published' else None)
            session.add(row)
            if row.status=='published': mapping.setdefault(row.source_url,row.id)
        for offer in package['offers']:
            session.add(Offer(tenant_id=tid,name=offer['name'],description=offer['description'],tags=offer['tags'],
                              price=offer.get('price'),knowledge_id=mapping.get(offer.get('source_url'))))
    return tid
