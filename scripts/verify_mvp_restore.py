"""Restore into a new isolated verification database; never replace the pilot database."""
import json
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit,unquote
import psycopg
from psycopg import sql
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mvp.config import settings,RUNTIME
cfg=settings();dsn=cfg['admin_database_url'].replace('postgresql+psycopg:','postgresql:');u=urlsplit(dsn)
parser=argparse.ArgumentParser();parser.add_argument('--backup');args=parser.parse_args()
dump=Path(args.backup).resolve() if args.backup else sorted((RUNTIME/'backups').glob('*.dump'))[-1]
name='salescare_restore_check_'+str(int(time.time()))
before={}
with psycopg.connect(dsn) as conn:
    for table in ('knowledge','conversations','messages','tasks','model_configs'):
        before[table]=conn.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(table))).fetchone()[0]
    original=conn.execute('SELECT tenant_id,id,accepted_at,deadline_at,sla FROM tasks ORDER BY tenant_id,id').fetchall()
with psycopg.connect(dsn.rsplit('/',1)[0]+'/postgres',autocommit=True) as conn:
    conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
pgbin=Path(cfg['postgres_home'])/'pgsql/bin'
env=dict(os.environ,PGPASSWORD=unquote(u.password))
start=time.monotonic()
subprocess.run([str(pgbin/'pg_restore.exe'),'-h',u.hostname,'-p',str(u.port or 5432),'-U',unquote(u.username),'-d',name,'--exit-on-error',str(dump)],env=env,check=True,capture_output=True)
with psycopg.connect(dsn.rsplit('/',1)[0]+'/'+name) as conn:
    after={table:conn.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(table))).fetchone()[0] for table in before}
    restored=conn.execute('SELECT tenant_id,id,accepted_at,deadline_at,sla FROM tasks ORDER BY tenant_id,id').fetchall()
assert before==after and original==restored,'Restore verification did not match the snapshot'
result={'ok':True,'backup':dump.name,'restored_database':name,'rows':after,'deadlines_match':True,'restore_seconds':round(time.monotonic()-start,2),
        'scope':'Snapshot row counts and stored deadlines; not a full new-host or vault recovery drill.'}
(RUNTIME/'restore-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
print(json.dumps(result,ensure_ascii=True))
