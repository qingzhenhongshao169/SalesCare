"""Prepare clearly labelled, synthetic browser verification in the separate test DB."""
import json
import os
import sys
import time
from pathlib import Path
from sqlalchemy import select
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
runtime=ROOT/'.mvp-runtime'
cfg=json.loads((runtime/'test-config.json').read_text('utf-8'))
cfg.update(mode='lan-pilot',public_origin='http://127.0.0.1:8768',cookie_suffix='_qa')
path=runtime/'ui-config.json';path.write_text(json.dumps(cfg),'utf-8')
os.environ['SALESCARE_MVP_CONFIG']=str(path)
from mvp.db import transaction
from mvp.models import *
from mvp.security import hash_password
with transaction() as session:
    tenant=session.scalar(select(Tenant).where(Tenant.slug=='ui-verification'))
    if not tenant:
        tenant=Tenant(slug='ui-verification',name='标梵互动 · 验收环境');session.add(tenant);session.flush()
tid=tenant.id
with transaction(tid) as session:
    if not session.get(CompanySettings,(tid,'primary')):
        session.add(CompanySettings(tenant_id=tid,id='primary',weekdays=list(range(7)),opens='00:00',closes='23:59',published=True,
                                    allowed_origins=['http://127.0.0.1:8768']))
        session.add(ModelConfig(tenant_id=tid,id='primary',config={}))
        package=json.loads((ROOT/'knowledge/biaofun.json').read_text('utf-8'))
        for k in package['entries']:
            session.add(Knowledge(tenant_id=tid,id=k['id'],family=k.get('family',k['id']),title=k['title'],content=k['content'],
                                  keywords=k.get('keywords',[]),source_url=k.get('source_url',''),category=k.get('category','企业知识'),
                                  status=k['status'],risk=k.get('risk','low'),author_id='curation',reviewer_id='verified-import'))
    if not session.scalar(select(Member).where(Member.tenant_id==tid,Member.username=='qa-admin')):
        session.add(Member(tenant_id=tid,username='qa-admin',name='验收管理员',password_hash=hash_password('synthetic-ui-test-password'),
                          roles=['admin','agent','editor','publisher'],skills=['数字营销与开发咨询'],available=True,heartbeat_at=time.time()))
    for username,name,roles in [('qa-agent1','验收客服一',['agent']),('qa-agent2','验收客服二',['agent','editor'])]:
        if not session.scalar(select(Member).where(Member.tenant_id==tid,Member.username==username)):
            session.add(Member(tenant_id=tid,username=username,name=name,password_hash=hash_password('synthetic-ui-test-password'),
                              roles=roles,skills=['数字营销与开发咨询'],capacity=5,available=True,heartbeat_at=time.time()))
    config=session.get(CompanySettings,(tid,'primary'))
    if 'http://127.0.0.1:8769' not in config.allowed_origins:
        config.allowed_origins=[*config.allowed_origins,'http://127.0.0.1:8769']
host=runtime/'widget-check';host.mkdir(exist_ok=True)
(host/'index.html').write_text('''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>SalesCare 网站嵌入验收</title>
<style>body{font:18px system-ui;background:#f2f6f5;color:#25413d;padding:50px}h1{font-size:32px}p{max-width:550px;line-height:1.8}</style>
<h1>企业网站 · 嵌入验收</h1><p>这是独立的网站页面，右下角的咨询按钮通过 Widget 打开企业客服。此环境只使用合成验收资料。</p>
<script src="http://127.0.0.1:8768/widget.js" data-company="ui-verification" defer></script></html>''','utf-8')
print('Synthetic UI verification prepared: enterprise ui-verification, account qa-admin.')
