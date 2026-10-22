"""Correlate a bounded browser run with its tenant's persisted usage events."""
import argparse
import json
import math
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
parser = argparse.ArgumentParser()
parser.add_argument('--config', required=True)
parser.add_argument('--report', required=True)
parser.add_argument('--tenant', required=True)
parser.add_argument('--output', required=True)
args = parser.parse_args()
os.environ['SALESCARE_MVP_CONFIG'] = str(Path(args.config).resolve())
from sqlalchemy import select
from mvp.db import transaction, rows
from mvp.models import Tenant, Usage, AIJob

report = json.loads(Path(args.report).read_text('utf-8'))
start = datetime.fromisoformat(report['started_at'].replace('Z', '+00:00')).timestamp()
with transaction() as session:
    tid = session.scalar(select(Tenant.id).where(Tenant.slug == args.tenant))
if not tid:
    raise SystemExit('Tenant not found')
with transaction(tid) as session:
    usage = list(session.scalars(rows(session, Usage, tid).where(Usage.created_at >= start)))
    queue = []
    for row in usage:
        job_id = (row.detail or {}).get('job_id')
        job = session.get(AIJob, (tid, job_id)) if job_id else None
        if job:
            queue.append((row.created_at - job.created_at) * 1000)
    queue.sort()
    data = {
        'date': '2026-10-15', 'started_at': report['started_at'],
        'scope': report['scope'],
        'browser_samples': report.get('samples', len(report.get('rows', []))),
        'usage_samples': len(usage),
        'outcomes': dict(Counter(row.outcome for row in usage)),
        'queue': {'samples': len(queue),
                  'p50_ms': round(queue[math.ceil(len(queue)*.5)-1], 1) if queue else None,
                  'p95_ms': round(queue[math.ceil(len(queue)*.95)-1], 1) if queue else None,
                  'max_ms': round(max(queue), 1) if queue else None},
    }
Path(args.output).write_text(json.dumps(data, ensure_ascii=False, indent=2), 'utf-8')
print(json.dumps(data, ensure_ascii=True))
