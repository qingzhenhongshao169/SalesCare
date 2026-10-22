"""Internal live smoke test through the existing Biaofun service; never prints credentials."""
import json
from pathlib import Path
import sys
import time
import uuid

import httpx
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mvp.db import transaction, rows
from mvp.models import Tenant, Conversation, Usage
from mvp.service import audit

with transaction() as s:
    tenant = s.scalar(select(Tenant).where(Tenant.slug == 'biaofun'))
result = {'environment': 'original_biaofun', 'synthetic_conversation': True,
          'mock_provider': False, 'turns': []}
with httpx.Client(base_url='http://127.0.0.1:8767', trust_env=False, timeout=10) as client:
    response = client.post('/api/public/session/biaofun')
    response.raise_for_status()
    auth = response.json()
    client.headers.update({'Authorization': 'Bearer ' + auth['token'], 'X-CSRF-Token': auth['csrf']})
    response = client.post('/api/public/conversations', json={})
    response.raise_for_status()
    cid = response.json()['id']
    with transaction(tenant.id) as s:
        s.get(Conversation, (tenant.id, cid)).name = '案例接入验证 · 内部测试'
        audit(s, tenant.id, 'local-operator', 'verification.case_live', cid, {'synthetic': True})
    for question in ['我们是一家激光设备制造企业，想做面向海外的官网。',
                     '希望让国外客户了解设备参数和品牌实力，首期内容应该怎么安排？',
                     '有没有做过类似工业外贸官网？请给我一个相关案例链接，并说明可参考什么。']:
        started = time.monotonic()
        response = client.post('/api/public/conversations/' + cid + '/messages',
                               json={'text': question, 'client_id': 'case-live-' + uuid.uuid4().hex})
        response.raise_for_status()
        sequence = response.json()['message']['sequence']
        reply = None
        while time.monotonic() - started < 45:
            response = client.get('/api/public/conversations/' + cid)
            response.raise_for_status()
            replies = [m for m in response.json()['messages'] if m['role'] == 'ai' and m['sequence'] > sequence]
            if replies:
                reply = replies[0]
                break
            time.sleep(.25)
        with transaction(tenant.id) as s:
            events = list(s.scalars(rows(s, Usage, tenant.id)))
            usage = [u for u in events if u.detail.get('conversation_id') == cid]
            last = max(usage, key=lambda u: u.created_at) if usage else None
        result['turns'].append({'question': question, 'answer': reply['text'] if reply else None,
                                'elapsed_ms': round((time.monotonic() - started) * 1000),
                                'case_count': len(reply.get('case_recommendations', [])) if reply else 0,
                                'citations': reply.get('citations', []) if reply else [],
                                'outcome': last.outcome if last else 'no_usage'})
        print(json.dumps({k: v for k, v in result['turns'][-1].items()
                          if k in ('elapsed_ms', 'case_count', 'outcome') }), flush=True)
        if not reply:
            break
result['conversation_id'] = cid
result['all_real_llm'] = len(result['turns']) == 3 and all(t['outcome'] == 'llm' for t in result['turns'])
(ROOT / 'docs/biaofun-case-live-check-20261019.json').write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print('all_real_llm', result['all_real_llm'])
