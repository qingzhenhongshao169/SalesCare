"""Copy encrypted model configuration for the same company, disabled pending revalidation."""
from pathlib import Path
import sqlite3
import json
import sys
from sqlalchemy import select
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mvp.db import transaction
from mvp.models import Tenant,ModelConfig
source=ROOT/'runtime/demo.sqlite3'
with sqlite3.connect('file:'+source.as_posix()+'?mode=ro',uri=True) as connection:
    record=connection.execute('SELECT document,secret FROM model_config WHERE tenant=?',('biaofun',)).fetchone()
if record:
    with transaction() as session:tenant=session.scalar(select(Tenant).where(Tenant.slug=='biaofun'))
    with transaction(tenant.id) as session:
        row=session.get(ModelConfig,(tenant.id,'primary'))
        if row.cipher:raise SystemExit('MVP already has a key; migration did not overwrite it.')
        config=json.loads(record[0]);config.update(enabled=False,tested_fingerprint=None,tested_at=None,last_test='not_tested')
        row.config=config;row.cipher=record[1]
    print('Same-company encrypted configuration copied; MVP requires a new connection test and explicit enable.')
else:print('No demo model configuration to migrate.')
