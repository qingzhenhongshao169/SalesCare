"""Fresh disposable PostgreSQL migration check; never touches pilot tables or outputs secrets."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
from urllib.parse import urlsplit,urlunsplit
import psycopg
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from server.model_gateway import protect
runtime=ROOT/'.mvp-runtime';base=json.loads((runtime/'config.json').read_text('utf-8'))
owner=protect(base['admin_database_url_sealed'],decrypt=True).replace('postgresql+psycopg:','postgresql:')
name='salescare_migration_smoke_'+uuid.uuid4().hex[:12]
assert re.fullmatch(r'salescare_migration_smoke_[a-f0-9]{12}',name)
u=urlsplit(owner);root_url=urlunsplit((u.scheme,u.netloc,'/postgres','',''))
with psycopg.connect(root_url,autocommit=True) as conn:conn.execute('CREATE DATABASE '+name)
try:
    config=dict(base)
    for key in ('admin_database_url','database_url'):
        value=protect(base[key+'_sealed'],decrypt=True);p=urlsplit(value)
        config[key+'_sealed']=protect(urlunsplit((p.scheme,p.netloc,'/'+name,'','')))
    path=runtime/'migration-smoke-config.json';path.write_text(json.dumps(config),'utf-8')
    env=dict(os.environ,SALESCARE_MVP_CONFIG=str(path))
    result=subprocess.run([sys.executable,'-m','alembic','-c','mvp/alembic.ini','upgrade','head'],cwd=ROOT,env=env,capture_output=True)
    if result.returncode:raise RuntimeError('Fresh migration failed; diagnostic output withheld to protect runtime credentials.')
    dburl=urlunsplit((u.scheme,u.netloc,'/'+name,'',''))
    with psycopg.connect(dburl) as conn:
        revision=conn.execute('SELECT version_num FROM alembic_version').fetchone()[0]
        count=conn.execute("SELECT count(*) FROM pg_class WHERE relrowsecurity AND relforcerowsecurity AND relnamespace='public'::regnamespace").fetchone()[0]
        defaults=conn.execute("SELECT count(*) FROM information_schema.columns WHERE (table_name='usage_events' AND column_name='detail' OR table_name='outbox_events' AND column_name='last_error') AND column_default IS NOT NULL").fetchone()[0]
        assert revision=='0009_service_mode' and count==28 and defaults==2
    output={'fresh_migration':True,'revision':revision,'force_rls_tables':count,'additive_defaults_preserve_old_inserts':True,'scope':'Disposable empty database; not a pilot data restore or disaster-recovery test.'}
    (ROOT/'docs/channel-migration-check.json').write_text(json.dumps(output,indent=2),'utf-8');print(json.dumps(output))
finally:
    # Only the empty, random database created above is eligible for cleanup.
    with psycopg.connect(root_url,autocommit=True) as conn:conn.execute('DROP DATABASE '+name)
