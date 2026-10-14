"""Bounded load against the synthetic localhost UI verification tenant."""
import asyncio
import json
import statistics
import time
from pathlib import Path
import httpx

URL='http://127.0.0.1:8768'
ROOT=Path(__file__).resolve().parents[1]

async def main():
    timings=[];visitors=[]
    async with httpx.AsyncClient(base_url=URL,trust_env=False,timeout=20,limits=httpx.Limits(max_connections=100,max_keepalive_connections=100)) as client:
        for _ in range(50):
            r=await client.post('/api/public/session/ui-verification');r.raise_for_status();auth=r.json()
            headers={'Authorization':'Bearer '+auth['token'],'X-CSRF-Token':auth['csrf']}
            r=await client.post('/api/public/conversations',json={},headers=headers);r.raise_for_status()
            visitors.append((r.json()['id'],headers))
        async def first_event(item):
            cid,headers=item
            started=time.perf_counter()
            async with client.stream('GET','/api/public/conversations/'+cid+'/events',headers=headers) as stream:
                stream.raise_for_status()
                async for line in stream.aiter_lines():
                    if line.startswith('data:'):
                        timings.append(time.perf_counter()-started)
                        return True
        connected=await asyncio.gather(*(first_event(item) for item in visitors))
        send_times=[]
        async def send(item):
            cid,headers=item;started=time.perf_counter()
            r=await client.post('/api/public/conversations/'+cid+'/messages',headers=headers,json={'text':'企业官网建设需要准备什么资料？','client_id':'load-one'})
            r.raise_for_status();send_times.append(time.perf_counter()-started)
            retry=await client.post('/api/public/conversations/'+cid+'/messages',headers=headers,json={'text':'企业官网建设需要准备什么资料？','client_id':'load-one'})
            retry.raise_for_status();assert retry.json()['duplicate']
            return True
        sent=await asyncio.gather(*(send(item) for item in visitors[:20]))
    def p95(values):return round(sorted(values)[max(0,__import__('math').ceil(len(values)*.95)-1)]*1000,1)
    result={'ok':all(connected) and all(sent),'event_connections':len(connected),'simultaneous_sends':len(sent),
            'event_first_data_p95_ms':p95(timings),'message_accept_p95_ms':p95(send_times),'retry_dedup':True,
            'scope':'Short local test with real PostgreSQL and synthetic data; not a sustained load or provider latency benchmark.'}
    (ROOT/'docs/mvp-load-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(result,ensure_ascii=True))

if __name__=='__main__':asyncio.run(main())
