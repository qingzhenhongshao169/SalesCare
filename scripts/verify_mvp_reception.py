"""Four synthetic identities with independent cookie jars on localhost QA only."""
import concurrent.futures
import json
from pathlib import Path
import time
import uuid
import httpx
ROOT=Path(__file__).resolve().parents[1]
URL='http://127.0.0.1:8768'
def request(client,method,path,body=None):
    r=client.request(method,path,json=body);r.raise_for_status();return r.json()
def employee(username):
    c=httpx.Client(base_url=URL,trust_env=False,timeout=10)
    auth=request(c,'POST','/api/login',{'tenant':'ui-verification','username':username,'password':'synthetic-ui-test-password'})
    c.headers['X-CSRF-Token']=auth['csrf'];return c,request(c,'GET','/api/me')
def visitor():
    c=httpx.Client(base_url=URL,trust_env=False,timeout=10)
    auth=request(c,'POST','/api/public/session/ui-verification');c.headers.update({'Authorization':'Bearer '+auth['token'],'X-CSRF-Token':auth['csrf']})
    cid=request(c,'POST','/api/public/conversations',{})['id'];return c,cid
def message(client,cid,text):return request(client,'POST','/api/public/conversations/'+cid+'/messages',{'text':text,'client_id':uuid.uuid4().hex})['message']
admin,am=employee('qa-admin');one,om=employee('qa-agent1');two,tm=employee('qa-agent2');v,cid=visitor()
try:
    for c in (admin,one,two):request(c,'POST','/api/presence',{'available':True})
    initial=message(v,cid,'验收需求：我们想做企业官网，需要准备哪些资料？');start=time.monotonic()
    while time.monotonic()-start<20:
        detail=request(v,'GET','/api/public/conversations/'+cid)
        if any(m['role']=='ai' and m['sequence']>initial['sequence'] for m in detail['messages']):break
        time.sleep(.5)
    else:raise RuntimeError('QA AI worker did not respond.')
    request(v,'POST','/api/public/conversations/'+cid+'/handoff',{})
    request(v,'POST','/api/public/conversations/'+cid+'/lead',{'name':'联调访客 · 合成','phone':'','consent':True,'schedule_at':None})
    task=request(admin,'GET','/api/conversations/'+cid)['task'];deadline=task['deadline_at']
    def claim(client):return client.post('/api/tasks/'+task['id']+'/actions/claim',json={'version':task['version']})
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:responses=list(pool.map(claim,(one,two)))
    assert sorted(r.status_code for r in responses)==[200,409]
    winning=0 if responses[0].status_code==200 else 1
    winner,other=(one,two) if winning==0 else (two,one);other_me=tm if winning==0 else om
    detail=request(winner,'GET','/api/conversations/'+cid);assert detail['task']['needs_first_reply']
    human=request(winner,'POST','/api/tasks/'+task['id']+'/contact',{'version':detail['task']['version'],'text':'验收消息：您好，我已查看您的官网需求。','client_id':'qa-human-first'})
    public=request(v,'GET','/api/public/conversations/'+cid);assert public['state']=='HUMAN_ACTIVE' and any(m['id']==human['id'] for m in public['messages'])
    request(v,'POST','/api/public/conversations/'+cid+'/received/'+str(human['sequence']),{})
    feedback=message(v,cid,'验收反馈：主要面向制造企业。')
    time.sleep(1)
    public=request(v,'GET','/api/public/conversations/'+cid)
    assert not any(m['role']=='ai' and m['sequence']>human['sequence'] for m in public['messages'])
    detail=request(winner,'GET','/api/conversations/'+cid)
    assert any(m['id']==feedback['id'] for m in detail['messages'])
    moved=request(winner,'POST','/api/tasks/'+task['id']+'/actions/transfer',{'version':detail['task']['version'],'owner_id':other_me['id']})
    assert moved['deadline_at']==deadline
    accepted=request(other,'POST','/api/tasks/'+task['id']+'/actions/claim',{'version':moved['version']})
    assert winner.post('/api/tasks/'+task['id']+'/contact',json={'version':moved['version'],'text':'应拒绝的旧接待人消息','client_id':'rejected-old-owner'}).status_code==409
    request(other,'POST','/api/tasks/'+task['id']+'/contact',{'version':accepted['version'],'text':'验收消息：我已接手，会继续为您说明。','client_id':'qa-human-second'})
    final=request(admin,'GET','/api/conversations/'+cid);assert final['task']['deadline_at']==deadline and final['task']['attempted_at']
    request(other,'POST','/api/tasks/'+task['id']+'/actions/close',{'version':final['task']['version']})
    # Leave a separate synthetic source-backed first-reply scenario for screenshot inspection.
    sample,sample_id=visitor()
    message(sample,sample_id,'截图验收：企业官网需要哪些资料？我们希望先做品牌展示。')
    request(sample,'POST','/api/public/conversations/'+sample_id+'/lead',{'name':'截图验收访客 · 合成','phone':'','consent':True,'schedule_at':None})
    current=request(admin,'GET','/api/conversations/'+sample_id)['task']
    request(admin,'POST','/api/tasks/'+current['id']+'/actions/claim',{'version':current['version']})
    result={'synthetic':True,'identities':[am['name'],om['name'],tm['name'],'匿名合成访客'],
            'independent_cookie_jars':True,'simultaneous_claims':[r.status_code for r in responses],
            'human_messages_received':True,'visitor_response_visible':True,'no_ai_after_takeover':True,
            'old_owner_reply_rejected':True,'deadline_preserved':True,'first_reply_stage_verified':True,
            'screenshot_conversation':sample_id,'scope':'Separate PostgreSQL test database and localhost QA server. HTTP identity isolation is verified; real colleagues must still perform their own browser UAT.'}
    (ROOT/'docs/optimization-reception-live.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(result,ensure_ascii=False));sample.close()
finally:
    for c in (admin,one,two,v):c.close()
