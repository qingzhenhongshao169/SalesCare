"""Integration tests use an isolated real PostgreSQL database, never the pilot data."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path
import psycopg
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from server.model_gateway import protect

runtime=ROOT/'.mvp-runtime'
base=json.loads(Path(os.environ.get('SALESCARE_TEST_BASE_CONFIG',runtime/'config.json')).read_text('utf-8'))
owner=protect(base['admin_database_url_sealed'],decrypt=True).replace('postgresql+psycopg:','postgresql:')
test_db=os.environ.get('SALESCARE_TEST_DATABASE_NAME','salescare_mvp_test')
assert re.fullmatch(r'salescare_(?:mvp_test|case_test_[a-f0-9]{12})',test_db), 'Only isolated test databases are allowed'
with psycopg.connect(owner.rsplit('/',1)[0]+'/postgres',autocommit=True,connect_timeout=5) as conn:
    if not conn.execute('SELECT 1 FROM pg_database WHERE datname=%s',(test_db,)).fetchone():conn.execute('CREATE DATABASE '+test_db)
test_config=dict(base)
for key in ('admin_database_url','database_url'):
    value=protect(base[key+'_sealed'],decrypt=True).rsplit('/',1)[0]+'/'+test_db
    test_config[key+'_sealed']=protect(value)
test_config['mode']='test'
config_file=runtime/('test-config.json' if test_db=='salescare_mvp_test' else test_db+'-config.json');config_file.write_text(json.dumps(test_config),'utf-8')
os.environ['SALESCARE_MVP_CONFIG']=str(config_file)
subprocess.run([sys.executable,'-m','alembic','-c','mvp/alembic.ini','upgrade','head'],cwd=ROOT,check=True,capture_output=True)

from fastapi.testclient import TestClient
from mvp.app import app
from mvp.db import transaction
from mvp.models import Tenant,CompanySettings,ModelConfig,Member,Knowledge,uid
from mvp.security import hash_password

@pytest.fixture
def make_tenant():
    def factory(name='测试企业'):
        with transaction() as session:
            t=Tenant(slug='test-'+uid()[:8],name=name);session.add(t);session.flush()
        with transaction(t.id) as session:
            session.add(CompanySettings(tenant_id=t.id,id='primary',weekdays=list(range(7)),opens='00:00',closes='23:59',published=True))
            session.add(ModelConfig(tenant_id=t.id,id='primary',config={}))
            session.add(Knowledge(tenant_id=t.id,id='faq',family='faq',title='企业官网建设',content='企业官网建设可以先提供企业介绍、产品服务资料、参考网站和希望上线的时间。',keywords=['官网','网站','建设'],status='published',author_id='editor',reviewer_id='publisher'))
        return t
    return factory

@pytest.fixture
def make_employee():
    def factory(t,roles=None,username=None):
        with transaction(t.id) as session:
            m=Member(tenant_id=t.id,username=username or 'user-'+uid()[:8],name='测试员工',password_hash=hash_password('synthetic-test-password-only'),
                     roles=roles or ['admin','agent','editor','publisher'],skills=['数字营销与开发咨询'],available=True,heartbeat_at=__import__('time').time())
            session.add(m);session.flush()
        client=TestClient(app,base_url='https://testserver')
        response=client.post('/api/login',json={'tenant':t.slug,'username':m.username,'password':'synthetic-test-password-only'})
        assert response.status_code==200,response.text
        client.headers['X-CSRF-Token']=response.json()['csrf']
        return client,m
    return factory

@pytest.fixture
def make_visitor():
    def factory(t):
        client=TestClient(app,base_url='https://testserver')
        response=client.post('/api/public/session/'+t.slug)
        assert response.status_code==200,response.text
        client.headers['X-CSRF-Token']=response.json()['csrf']
        client.headers['Authorization']='Bearer '+response.json()['token']
        c=client.post('/api/public/conversations',json={})
        assert c.status_code==200,c.text
        return client,c.json()['id']
    return factory
