"""Fresh and incremental migration exercise in a disposable DB on the owned QA cluster."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
from urllib.parse import urlsplit,urlunsplit
import psycopg
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from server.model_gateway import protect
from mvp.models import Tenant,CompanySettings,Conversation,Message,Knowledge

runtime=ROOT/'.mvp-runtime';base=json.loads((runtime/'case-test-postgres-20261019/base-config.json').read_text('utf-8'))
state=json.loads((runtime/'case-test-postgres-20261019/connection.json').read_text('utf-8'))
owner=protect(base['admin_database_url_sealed'],decrypt=True).replace('postgresql+psycopg:','postgresql:')
parsed=urlsplit(owner)
assert parsed.hostname=='127.0.0.1' and parsed.port==state['port']
name='salescare_case_migration_'+uuid.uuid4().hex[:12]
assert re.fullmatch(r'salescare_case_migration_[a-f0-9]{12}',name)
with psycopg.connect(owner,autocommit=True,connect_timeout=5) as c:c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
target=urlunsplit((parsed.scheme,parsed.netloc,'/'+name,'',''))
config=dict(base)
for key in ('database_url','admin_database_url'):
    value=urlsplit(protect(base[key+'_sealed'],decrypt=True))
    config[key+'_sealed']=protect(urlunsplit((value.scheme,value.netloc,'/'+name,'','')))
config_path=runtime/'case-migration-config.json';config_path.write_text(json.dumps(config),encoding='utf-8')
env={**os.environ,'SALESCARE_MVP_CONFIG':str(config_path),'PYTHONIOENCODING':'utf-8'}
def migrate():
    r=subprocess.run([sys.executable,'-m','alembic','-c','mvp/alembic.ini','upgrade','head'],cwd=ROOT,env=env,capture_output=True,timeout=60)
    if r.returncode:raise RuntimeError('Isolated migration failed; no connection details emitted')
try:
    migrate()
    engine=create_engine(target.replace('postgresql:','postgresql+psycopg:'),hide_parameters=True)
    with Session(engine) as s,s.begin():
        s.add(Tenant(id='legacy-tenant',slug='legacy-case-test',name='Synthetic legacy tenant'));s.flush()
        s.add(CompanySettings(tenant_id='legacy-tenant',id='primary'))
        s.add(Conversation(tenant_id='legacy-tenant',id='legacy-conversation',visitor_id='synthetic'));s.flush()
        s.add(Message(tenant_id='legacy-tenant',id='legacy-message',conversation_id='legacy-conversation',sequence=1,role='visitor',text='legacy message preserved'))
        s.add(Knowledge(tenant_id='legacy-tenant',id='legacy-knowledge',family='legacy',title='legacy',content='legacy knowledge preserved',author_id='synthetic'))
    engine.dispose()
    with psycopg.connect(target,autocommit=True,connect_timeout=5) as c:
        assert c.execute('SELECT current_database()').fetchone()[0]==name
        fresh_revision=c.execute('SELECT version_num FROM alembic_version').fetchone()[0]
        # Only this new disposable DB is rewound to a pre-0012 schema; no pilot DB is connected.
        for table,column in [('knowledge','case_recommendation'),('messages','case_recommendations'),('company_settings','case_recommendation_policy')]:
            c.execute(sql.SQL('ALTER TABLE {} DROP COLUMN {}').format(sql.Identifier(table),sql.Identifier(column)))
        c.execute("UPDATE alembic_version SET version_num='0011_quick_reply'")
    migrate()
    with psycopg.connect(target,connect_timeout=5) as c:
        assert c.execute("SELECT content,case_recommendation FROM knowledge WHERE id='legacy-knowledge'").fetchone()==('legacy knowledge preserved',{})
        assert c.execute("SELECT text,case_recommendations FROM messages WHERE id='legacy-message'").fetchone()==('legacy message preserved',[])
        assert c.execute("SELECT case_recommendation_policy FROM company_settings WHERE tenant_id='legacy-tenant'").fetchone()[0]=={}
        c.execute("INSERT INTO messages (tenant_id,id,created_at,conversation_id,sequence,client_id,role,actor_id,actor_name,text,citations,received_at) SELECT tenant_id,'legacy-copy',created_at,conversation_id,sequence+1,NULL,role,actor_id,actor_name,text,citations,received_at FROM messages WHERE id='legacy-message'")
        assert c.execute("SELECT case_recommendations FROM messages WHERE id='legacy-copy'").fetchone()[0]==[]
        count=c.execute("SELECT count(*) FROM pg_class WHERE relrowsecurity AND relforcerowsecurity AND relnamespace='public'::regnamespace").fetchone()[0]
        assert count==28 and fresh_revision=='0012_case_recommendations'
        report={'fresh_migration':True,'incremental_from':'0011_quick_reply','revision':fresh_revision,'legacy_rows_preserved':True,
                'old_message_insert_compatible':True,'force_rls_tables':count,'pilot_database_accessed':False}
    (ROOT/'docs/case-recommendation-migration-check.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))
finally:
    with psycopg.connect(owner,autocommit=True,connect_timeout=5) as c:c.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
