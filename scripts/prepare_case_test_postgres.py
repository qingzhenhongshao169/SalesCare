"""Create a loopback-only disposable cluster; never start or change the pilot cluster."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
from urllib.parse import quote
import psycopg
from psycopg import sql

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from server.model_gateway import protect

runtime=ROOT/'.mvp-runtime'
home=runtime/'case-test-postgres-20261019'
manifest=home/'connection.json'
home.mkdir(parents=True,exist_ok=True)
cluster=Path(os.environ['LOCALAPPDATA'])/'SalesCareCaseQA-20261019'
cluster.mkdir(parents=True,exist_ok=True)
# Windows PostgreSQL bootstrap needs ASCII binary/data paths with UTF8 and locale C.
binaries=Path(os.environ['LOCALAPPDATA'])/'SalesCareMVP/pgsql/bin'
if not (binaries/'initdb.exe').exists():
    import shutil
    shutil.copytree(runtime/'pgsql',cluster/'pgsql',dirs_exist_ok=True)
    binaries=cluster/'pgsql/bin'
if manifest.exists():
    state=json.loads(manifest.read_text('utf-8'))
else:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    state={'port':port,'owner_sealed':protect(secrets.token_urlsafe(32)),
           'app_sealed':protect(secrets.token_urlsafe(32))}
    manifest.write_text(json.dumps(state),encoding='utf-8')
data=cluster/'data'
owner=protect(state['owner_sealed'],decrypt=True)
app=protect(state['app_sealed'],decrypt=True)

def command(args):
    with (home/(args[0]+'.log')).open('wb') as log:
        result=subprocess.run([str(binaries/args[0]),*args[1:]],stdout=log,stderr=log,timeout=60)
    if result.returncode:
        raise RuntimeError(args[0]+' failed; output retained only in the isolated cluster log')

if not (data/'PG_VERSION').exists():
    pw=cluster/'init-password.tmp';pw.write_text(owner+'\n',encoding='utf-8')
    try:command(['initdb.exe','-D',str(data),'-U','case_qa_owner','--pwfile',str(pw),'-A','scram-sha-256','--encoding=UTF8','--locale=C'])
    finally:pw.unlink(missing_ok=True)
status=subprocess.run([str(binaries/'pg_ctl.exe'),'-D',str(data),'status'],capture_output=True)
if status.returncode:
    command(['pg_ctl.exe','-D',str(data),'-l',str(cluster/'postgres.log'),'-o',f'-p {state["port"]} -h 127.0.0.1','-w','-t','20','start'])
url=f'postgresql://case_qa_owner:{quote(owner,safe="")}@127.0.0.1:{state["port"]}/postgres'
with psycopg.connect(url,autocommit=True,connect_timeout=5) as conn:
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='salescare_app'").fetchone():
        conn.execute(sql.SQL('CREATE ROLE salescare_app LOGIN PASSWORD {}').format(sql.Literal(app)))
config={'admin_database_url_sealed':protect(url.replace('postgresql:','postgresql+psycopg:')),
        'database_url_sealed':protect(f'postgresql+psycopg://salescare_app:{quote(app,safe="")}@127.0.0.1:{state["port"]}/postgres'),
        'mode':'test','public_origin':'https://testserver','cookie_suffix':'_case_test',
        'setup_token_sealed':protect(secrets.token_urlsafe(32))}
path=home/'base-config.json';path.write_text(json.dumps(config),encoding='utf-8')
print(json.dumps({'isolated_cluster_ready':True,'loopback_port':state['port'],'pilot_cluster_changed':False}))
