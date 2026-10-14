"""Create or verify backups without exposing database credentials."""
import argparse
from pathlib import Path
import os
import subprocess
import sys
import zipfile
import hashlib
import psycopg
from urllib.parse import urlsplit,unquote
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mvp.config import settings,RUNTIME
parser=argparse.ArgumentParser();parser.add_argument('--output');args=parser.parse_args()
cfg=settings();url=urlsplit(cfg['admin_database_url'].replace('postgresql+psycopg:','postgresql:'))
pgbin=Path(cfg['postgres_home'])/'pgsql/bin'
output=Path(args.output).resolve() if args.output else RUNTIME/'backups'/(__import__('time').strftime('salescare-%Y%m%d-%H%M%S.dump'))
output.parent.mkdir(parents=True,exist_ok=True)
env=dict(os.environ,PGPASSWORD=unquote(url.password))
originals=output.with_suffix('.documents.zip')
# One exported snapshot keeps the dump and the list of immutable originals aligned.
# If a concurrent deletion removes an original before copying, fail the backup;
# the supervisor retries rather than silently claiming a complete backup.
with psycopg.connect(cfg['admin_database_url'].replace('postgresql+psycopg:','postgresql:')) as conn:
    conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
    snapshot=conn.execute('SELECT pg_export_snapshot()').fetchone()[0]
    table=conn.execute("SELECT to_regclass('public.kb_document_versions')").fetchone()[0]
    files=conn.execute('SELECT v.storage_key,v.digest FROM kb_document_versions v JOIN kb_documents d ON v.tenant_id=d.tenant_id AND v.document_id=d.id WHERE NOT d.deleted').fetchall() if table else []
    root=(RUNTIME/'documents').resolve()
    with zipfile.ZipFile(originals,'w',zipfile.ZIP_DEFLATED) as archive:
        for key,digest in files:
            path=(root/key).resolve()
            if root not in path.parents:raise RuntimeError('Original storage path invalid; backup incomplete.')
            try:data=path.read_bytes()
            except OSError:raise RuntimeError('Original changed during backup; retry required.') from None
            if hashlib.sha256(data).hexdigest()!=digest:raise RuntimeError('Original digest mismatch; backup incomplete.')
            archive.writestr(key,data)
    subprocess.run([str(pgbin/'pg_dump.exe'),'-h',url.hostname,'-p',str(url.port or 5432),'-U',unquote(url.username),'-d',url.path.lstrip('/'),'--snapshot='+snapshot,'-Fc','-f',str(output)],env=env,check=True,capture_output=True)
print('Database backup created:',output.name)
print('Matching document originals backed up:',len(files))
