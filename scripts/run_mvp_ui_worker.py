"""Local UI verification worker scoped strictly to its named synthetic enterprise."""
from pathlib import Path
import os
import sys
import time
from sqlalchemy import select
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
os.environ['SALESCARE_MVP_CONFIG']=str(ROOT/'.mvp-runtime/ui-config.json')
from mvp.db import transaction
from mvp.models import Tenant
from mvp.worker import heartbeat,lease_ai,run_ai,tick_sla,deliver_notice
with transaction() as s:tid=s.scalar(select(Tenant.id).where(Tenant.slug=='ui-verification'))
if not tid:raise SystemExit('Prepare the synthetic UI enterprise first.')
while True:
    heartbeat();tick_sla(tid);deliver_notice(tid)
    job=lease_ai(tid)
    if job:run_ai(tid,job)
    time.sleep(1)
