"""Independent ingestion process; heavy parsing cannot block the reception timer."""
import logging
import signal
import threading
import time
from sqlalchemy import select
from .db import transaction,rows,locked
from .models import Tenant,KBJob,WorkerState
from .knowledge_documents import process
STOP=threading.Event()

def lease(tid):
    with transaction(tid) as s:
        job=s.scalar(rows(s,KBJob,tid).where(KBJob.status.in_(['pending','leased']),KBJob.lease_until<time.time()).order_by(KBJob.created_at).with_for_update(skip_locked=True).limit(1))
        if not job:return None
        if job.attempts>=3:job.status='failed';job.error='failed';return None
        job.status='leased';job.attempts+=1;job.lease_until=time.time()+300
        return job.id

def main():
    signal.signal(signal.SIGINT,lambda *_:STOP.set());signal.signal(signal.SIGTERM,lambda *_:STOP.set())
    def beat():
        while not STOP.is_set():
            try:
                with transaction() as s:
                    row=s.get(WorkerState,'knowledge-ingestion')
                    if row:row.heartbeat_at=time.time()
                    else:s.add(WorkerState(id='knowledge-ingestion',heartbeat_at=time.time()))
            except Exception:pass
            STOP.wait(3)
    threading.Thread(target=beat,daemon=True).start()
    while not STOP.is_set():
        try:
            with transaction() as s:tenants=list(s.scalars(select(Tenant.id).where(Tenant.active==True)))
            for tid in tenants:
                jid=lease(tid)
                if jid:process(tid,jid)
        except Exception:logging.error('Document worker cycle failed; durable retry retained.')
        STOP.wait(1)

if __name__=='__main__':main()
