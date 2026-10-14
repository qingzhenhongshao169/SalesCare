"""Local PostgreSQL provisioning; no credentials are printed or put in command arguments."""
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote
import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from server.model_gateway import protect

runtime = ROOT / '.mvp-runtime'
runtime.mkdir(exist_ok=True)
pg_home = Path(os.environ['LOCALAPPDATA']) / 'SalesCareMVP'
pg_home.mkdir(exist_ok=True)
if not (pg_home/'pgsql/bin/initdb.exe').exists():
    shutil.copytree(runtime/'pgsql',pg_home/'pgsql',dirs_exist_ok=True)
bin_dir = pg_home / 'pgsql' / 'bin'
data = pg_home / 'pgdata'
config_path = runtime / 'config.json'
if not (bin_dir/'initdb.exe').exists():
    raise SystemExit('Run scripts/download_mvp_postgres.py first')

if config_path.exists():
    config = json.loads(config_path.read_text('utf-8'))
    owner_url = protect(config['admin_database_url_sealed'],decrypt=True).replace('postgresql+psycopg:','postgresql:')
    if config.get('provisioning'): owner_url=owner_url.rsplit('/',1)[0]+'/postgres'
else:
    owner_pw = secrets.token_urlsafe(40)
    app_pw = secrets.token_urlsafe(40)
    pw_file = pg_home/'initial-db-password.tmp'
    pw_file.write_text(owner_pw,'utf-8')
    username = os.environ.get('USERNAME','')
    subprocess.run(['icacls',str(runtime),'/inheritance:r','/grant:r',f'{username}:(OI)(CI)F','SYSTEM:(OI)(CI)F'],check=True,capture_output=True)
    subprocess.run(['icacls',str(pg_home),'/inheritance:r','/grant:r',f'{username}:(OI)(CI)F','SYSTEM:(OI)(CI)F'],check=True,capture_output=True)
    try:
        result = subprocess.run([str(bin_dir/'initdb.exe'),'-D',str(data),'-U','salescare_owner',
                        '--auth-local=scram-sha-256','--auth-host=scram-sha-256','--encoding=UTF8','--locale=C',
                        '--pwfile='+str(pw_file)],capture_output=True,text=True)
        if result.returncode: raise RuntimeError('PostgreSQL initialization failed: '+result.stderr[:500])
    finally: pw_file.unlink(missing_ok=True)
    with (data/'postgresql.conf').open('a',encoding='utf-8') as file:
        file.write("\nlisten_addresses='127.0.0.1'\nport=55432\nmax_connections=100\n")
    owner_url = 'postgresql://salescare_owner:'+quote(owner_pw,safe='')+'@127.0.0.1:55432/postgres'
    config = dict(mode='lan-pilot',public_origin='http://192.168.0.236:8767',
                  admin_username='admin1',admin_name='管理员1',postgres_home=str(pg_home),
                  admin_database_url_sealed=protect(owner_url.replace('/postgres','/salescare_mvp').replace('postgresql:','postgresql+psycopg:')),
                  database_url_sealed=protect('postgresql+psycopg://salescare_app:'+quote(app_pw,safe='')+'@127.0.0.1:55432/salescare_mvp'),
                  setup_token_sealed=protect(secrets.token_urlsafe(40)),provisioning=True)
    config_path.write_text(json.dumps(config,ensure_ascii=False,indent=2),'utf-8')

status = subprocess.run([str(bin_dir/'pg_ctl.exe'),'status','-D',str(data)],capture_output=True)
if status.returncode:
    with (pg_home/'launch.log').open('a') as log:
        launch = subprocess.run([str(bin_dir/'pg_ctl.exe'),'start','-D',str(data),'-l',str(pg_home/'postgres.log'),'-w'],stdout=log,stderr=log,timeout=40)
    if launch.returncode: raise RuntimeError('Local database could not start; see launch.log')

if config.get('provisioning'):
    owner_pw = protect(config['admin_database_url_sealed'],decrypt=True).split('://',1)[1].split('@',1)[0].split(':',1)[1]
    app_pw = protect(config['database_url_sealed'],decrypt=True).split('://',1)[1].split('@',1)[0].split(':',1)[1]
    with psycopg.connect(owner_url,autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='salescare_app'").fetchone():
            conn.execute(sql.SQL('CREATE ROLE salescare_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD {}').format(sql.Literal(app_pw)))
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname='salescare_mvp'").fetchone():
            conn.execute('CREATE DATABASE salescare_mvp')
    config.pop('provisioning',None)
    config_path.write_text(json.dumps(config,ensure_ascii=False,indent=2),'utf-8')
subprocess.run([sys.executable,'-m','alembic','-c','mvp/alembic.ini','upgrade','head'],cwd=ROOT,check=True)
print('MVP PostgreSQL ready on localhost:55432; runtime role has no RLS bypass.')
