"""Channels use synthetic credentials and an isolated real PostgreSQL database."""
import base64,json,struct,time,hashlib
import pytest
from sqlalchemy import select,func,text
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
from mvp.db import transaction,rows
from mvp.models import *
from mvp.channel_messages import ingest,prepare_delivery,complete_delivery
from mvp.channel_transport import PlatformError,decrypt_callback,platform_url
from mvp.security import digest
from mvp.config import settings

def config(kind='web'):
    if kind=='web':return {'name':'合成网站','kind':kind,'config':{'allowed_origins':['https://merchant.example'],'welcome':'欢迎咨询测试企业。'},'credentials':{}}
    if kind=='dingtalk':return {'name':'合成钉钉','kind':kind,'config':{'client_id':'synthetic-app-'+uid()},'credentials':{'client_secret':'synthetic-secret-only'}}
    if kind=='wecom_bot':return {'name':'合成企微','kind':kind,'config':{'bot_id':'synthetic-bot-'+uid()},'credentials':{'secret':'synthetic-secret-only'}}
    return {'name':'合成微信','kind':kind,'config':{'corp_id':'synthetic-corp-'+uid(),'open_kf_id':'synthetic-kf-'+uid()},'credentials':{'secret':'synthetic-secret-only','token':'synthetic-token','encoding_aes_key':base64.b64encode(b'x'*32).decode().rstrip('=')}}

def create(client,monkeypatch,kind='web'):
    monkeypatch.setattr('mvp.channel_transport.check_credentials',lambda *_:(True,''))
    response=client.post('/api/channels',json=config(kind));assert response.status_code==200,response.text
    ch=response.json();assert client.post('/api/channels/'+ch['id']+'/test',json={'version':ch['version']}).json()['tested']
    if kind!='wechat_kf':assert client.post('/api/channels/'+ch['id']+'/enable',json={'version':ch['version']}).status_code==200
    return ch

def frame(mid=None,sender='synthetic-member',text='企业官网需要准备什么？',room='synthetic-room'):
    return {'type':'CALLBACK','headers':{'topic':'/v1.0/im/bot/messages/get','messageId':mid or uid()},'data':{'msgId':mid or uid(),'msgtype':'text','text':{'content':text},'senderStaffId':sender,'conversationId':room,'conversationType':'2','sessionWebhook':'https://oapi.dingtalk.com/robot/sendBySession?session=synthetic-session','sessionWebhookExpiredTime':int((time.time()+3600)*1000)}}

@pytest.mark.parametrize('roles',[['agent'],['editor'],['publisher']])
def test_config_admin_only(make_tenant,make_employee,roles):
    c,_=make_employee(make_tenant(),roles)
    assert c.get('/api/channels').status_code==403
    assert c.get('/api/channel-types').status_code==403
    assert c.post('/api/channels',json=config()).status_code==403

@pytest.mark.parametrize('kind',['web','dingtalk','wecom_bot','wechat_kf'])
def test_config_is_private_and_tenant_scoped(make_tenant,make_employee,monkeypatch,kind):
    t=make_tenant();c,_=make_employee(t);other,_=make_employee(make_tenant());ch=create(c,monkeypatch,kind)
    assert not other.get('/api/channels').json()
    assert other.put('/api/channels/'+ch['id'],json={**config(kind),'version':ch['version']}).status_code==404
    assert other.get('/api/channels/'+ch['id']+'/deliveries').status_code==404
    assert 'synthetic-secret-only' not in c.get('/api/channels').text
    assert 'cipher' not in c.get('/api/channels').text
    with transaction(t.id) as s:
        saved=s.get(Channel,(t.id,ch['id']))
        assert 'synthetic-secret-only' not in saved.cipher

def test_application_unique_across_tenants_and_changed_identity_requires_secret(make_tenant,make_employee,monkeypatch):
    c,_=make_employee(make_tenant());other,_=make_employee(make_tenant());ch=create(c,monkeypatch,'dingtalk')
    body=config('dingtalk');body['config']['client_id']=ch['config']['client_id']
    assert other.post('/api/channels',json=body).status_code==409
    body.update(version=ch['version']);body['config']['client_id']='changed-app';body['credentials']={}
    assert c.put('/api/channels/'+ch['id'],json=body).status_code==422
    body['config']['client_id']=ch['config']['client_id'];response=c.put('/api/channels/'+ch['id'],json=body)
    assert response.status_code==200 and not response.json()['enabled'] and not response.json()['tested']
    assert c.post('/api/channels/'+ch['id']+'/enable',json={'version':ch['version']}).status_code==409

def test_channel_instance_visitor_isolation_and_frame_policy(make_tenant,make_employee,monkeypatch):
    from fastapi.testclient import TestClient
    from mvp.app import app
    t=make_tenant();c,_=make_employee(t);a=create(c,monkeypatch);b=create(c,monkeypatch)
    visitors=[]
    for ch in (a,b):
        v=TestClient(app,base_url='https://testserver');r=v.post('/api/public/session/'+t.slug+'?channel='+ch['id']).json()
        v.headers.update({'Authorization':'Bearer '+r['token'],'X-CSRF-Token':r['csrf']})
        cid=v.post('/api/public/conversations',json={}).json()['id'];visitors.append((v,cid))
        assert v.get('/api/public/me').json()['channel_id']==ch['id']
        assert 'https://merchant.example' in v.get('/chat/'+t.slug+'?channel='+ch['id']).headers['content-security-policy']
    assert visitors[0][0].get('/api/public/conversations/'+visitors[1][1]).status_code==404
    assert c.post('/api/channels/'+a['id']+'/disable',json={'version':a['version']}).status_code==200
    assert visitors[0][0].post('/api/public/conversations/'+visitors[0][1]+'/messages',json={'text':'你好','client_id':uid()}).status_code==409
    assert visitors[1][0].post('/api/public/conversations/'+visitors[1][1]+'/messages',json={'text':'你好','client_id':uid()}).status_code==200

def test_ingress_dedup_and_group_sender_separation(make_tenant,make_employee,monkeypatch):
    t=make_tenant();c,_=make_employee(t);ch=create(c,monkeypatch,'dingtalk');f=frame()
    cid,dup=ingest(t.id,ch['id'],ch['version'],f);assert not dup
    assert ingest(t.id,ch['id'],ch['version'],f)==(cid,True)
    cid2,_=ingest(t.id,ch['id'],ch['version'],frame(sender='another-member'));assert cid2!=cid
    with transaction(t.id) as s:
        assert s.scalar(select(func.count()).select_from(Message).where(Message.tenant_id==t.id,Message.conversation_id==cid))==1
        assert s.scalar(select(func.count()).select_from(AIJob).where(AIJob.tenant_id==t.id,AIJob.conversation_id==cid))==1
    c.post('/api/channels/'+ch['id']+'/disable',json={'version':ch['version']})
    with pytest.raises(PlatformError):ingest(t.id,ch['id'],ch['version'],frame())

def test_external_contact_is_counted_only_after_platform_acceptance(make_tenant,make_employee,monkeypatch):
    t=make_tenant();c,m=make_employee(t);ch=create(c,monkeypatch,'dingtalk');cid,_=ingest(t.id,ch['id'],ch['version'],frame(text='转人工'))
    with transaction(t.id) as s:
        task=s.scalar(rows(s,Task,t.id).where(Task.conversation_id==cid));taskid=task.id;version=task.version
        for d in s.scalars(rows(s,ChannelDelivery,t.id)):d.status='cancelled'
    claimed=c.post('/api/tasks/'+taskid+'/actions/claim',json={'version':version}).json()
    reply={'text':'您好，我来为您确认需求。','client_id':uid(),'version':claimed['version']}
    response=c.post('/api/tasks/'+taskid+'/contact',json=reply);assert response.status_code==200 and response.json()['delivery_status']=='pending'
    assert c.post('/api/tasks/'+taskid+'/contact',json=reply).json()['delivery_status']=='pending'
    with transaction(t.id) as s:assert s.get(Task,(t.id,taskid)).attempted_at is None
    item=prepare_delivery(t.id,ch['id']);assert item
    complete_delivery(t.id,item)
    with transaction(t.id) as s:
        assert s.get(Task,(t.id,taskid)).attempted_at is not None
        assert s.scalar(rows(s,ContactAttempt,t.id)).evidence=='platform_api_accepted'
    complete_delivery(t.id,item)
    with transaction(t.id) as s:assert s.scalar(select(func.count()).select_from(ContactAttempt).where(ContactAttempt.tenant_id==t.id))==1

@pytest.mark.parametrize('error',['timeout','network','rate_limited','connect_failed','provider'])
def test_external_failures_do_not_falsify_delivery(make_tenant,make_employee,monkeypatch,error):
    from mvp.service import add_message
    t=make_tenant();c,_=make_employee(t);ch=create(c,monkeypatch,'dingtalk');cid,_=ingest(t.id,ch['id'],ch['version'],frame())
    with transaction(t.id) as s:add_message(s,s.get(Conversation,(t.id,cid)),'ai','测试回答')
    item=prepare_delivery(t.id,ch['id']);complete_delivery(t.id,item,error)
    with transaction(t.id) as s:
        d=s.get(ChannelDelivery,(t.id,item['id']))
        assert d.accepted_at is None
        assert d.status==('uncertain' if error in ('timeout','network') else 'retry' if error in ('rate_limited','connect_failed') else 'failed')

def test_ai_delivery_cancelled_after_human_takeover(make_tenant,make_employee,monkeypatch):
    from mvp.service import add_message
    t=make_tenant();c,_=make_employee(t);ch=create(c,monkeypatch,'dingtalk');cid,_=ingest(t.id,ch['id'],ch['version'],frame())
    with transaction(t.id) as s:
        convo=s.get(Conversation,(t.id,cid));add_message(s,convo,'ai','应被抑制');convo.state='HUMAN_ACTIVE'
    assert prepare_delivery(t.id,ch['id']) is None
    with transaction(t.id) as s:assert s.scalar(rows(s,ChannelDelivery,t.id)).status=='cancelled'

def encrypted_callback(cfg,secret,plaintext):
    key=base64.b64decode(secret['encoding_aes_key']+'=');plain=b'r'*16+struct.pack('!I',len(plaintext))+plaintext+cfg['corp_id'].encode();padding=32-len(plain)%32;plain+=bytes([padding])*padding
    enc=Cipher(algorithms.AES(key),modes.CBC(key[:16])).encryptor();cipher=base64.b64encode(enc.update(plain)+enc.finalize()).decode()
    timestamp=str(int(time.time()));nonce='synthetic-nonce';signature=hashlib.sha1(''.join(sorted([secret['token'],timestamp,nonce,cipher])).encode()).hexdigest()
    return {'echostr':cipher,'timestamp':timestamp,'nonce':nonce,'msg_signature':signature}

def test_wechat_callback_verification_and_wrong_signature(make_tenant,make_employee,monkeypatch):
    t=make_tenant();c,_=make_employee(t);body=config('wechat_kf');r=c.post('/api/channels',json=body);ch=r.json();q=encrypted_callback(body['config'],body['credentials'],b'echo-ok');url='/api/channel-callback/'+t.slug+'/'+ch['id']
    assert c.get(url,params=q).text=='echo-ok'
    q['msg_signature']='wrong';assert c.get(url,params=q).status_code==403
    assert c.post(url,params=q,content='<xml><Encrypt>bad</Encrypt></xml>').status_code==403
    assert c.post('/api/channels/'+ch['id']+'/enable',json={'version':ch['version']}).status_code==409

def test_channel_rls_on_all_new_tables(make_tenant):
    t=make_tenant();other=make_tenant()
    with transaction(t.id) as s:
        ch=Channel(tenant_id=t.id,name='RLS',kind='web');s.add(ch);s.flush();cid=ch.id
    with transaction(other.id) as s:assert s.get(Channel,(t.id,cid)) is None
    with transaction() as s:
        flags=s.execute(text("SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname IN ('channels','external_threads','channel_receipts','channel_deliveries')")).all()
        assert len(flags)==4 and all(a and b for _,a,b in flags)

def test_platform_operator_is_local_and_not_tenant_admin(make_tenant,make_employee):
    c,_=make_employee(make_tenant())
    assert c.get('/api/platform/tenants').status_code==403
    token=c.get('/api/platform/local-token').json()['token'];assert token
    c.headers['X-Operator-Token']=token
    body={'slug':'new-'+uid()[:8],'name':'新测试企业','username':'admin1','admin_name':'新管理员'}
    r=c.post('/api/platform/tenants',json=body);assert r.status_code==200,r.text
    assert '#'+r.json()['invite_url'].split('#')[-1] in r.json()['invite_url']
    assert c.post('/api/platform/tenants',json=body).status_code==409
    with transaction() as s:t=s.scalar(select(Tenant).where(Tenant.slug==body['slug']))
    with transaction(t.id) as s:
        assert not list(s.scalars(rows(s,Knowledge,t.id)))
        assert s.get(CompanySettings,(t.id,'primary')).source_hosts==[]

@pytest.mark.parametrize('url',['http://oapi.dingtalk.com/robot/sendBySession','https://evil.example/robot/sendBySession','https://oapi.dingtalk.com@evil.example/robot/sendBySession','https://oapi.dingtalk.com/other'])
def test_untrusted_reply_destinations_rejected(url):
    with pytest.raises(PlatformError):platform_url(url,'oapi.dingtalk.com','/robot/sendBySession')

def test_unknown_media_gets_guidance_without_llm_job(make_tenant,make_employee,monkeypatch):
    t=make_tenant();c,_=make_employee(t);ch=create(c,monkeypatch,'dingtalk');f=frame();f['data']['msgtype']='image'
    cid,_=ingest(t.id,ch['id'],ch['version'],f)
    with transaction(t.id) as s:
        assert not s.scalar(rows(s,AIJob,t.id).where(AIJob.conversation_id==cid))
        assert s.scalar(rows(s,Message,t.id).where(Message.conversation_id==cid,Message.role=='ai')).text.startswith('目前可以通过文字咨询')

def test_gateway_lease_and_rotation_prevent_old_connections(make_tenant,make_employee,monkeypatch):
    from mvp.channel_gateway import acquire,renew
    t=make_tenant();c,_=make_employee(t);ch=create(c,monkeypatch,'dingtalk');item=acquire(t.id,ch['id']);assert item and renew(item)
    assert acquire(t.id,ch['id']) is None
    c.post('/api/channels/'+ch['id']+'/disable',json={'version':ch['version']})
    assert not renew(item)

def test_ambiguous_send_lease_is_not_automatically_resent(make_tenant,make_employee,monkeypatch):
    from mvp.service import add_message
    t=make_tenant();c,_=make_employee(t);ch=create(c,monkeypatch,'dingtalk');cid,_=ingest(t.id,ch['id'],ch['version'],frame())
    with transaction(t.id) as s:add_message(s,s.get(Conversation,(t.id,cid)),'ai','测试回答')
    item=prepare_delivery(t.id,ch['id'])
    with transaction(t.id) as s:s.get(ChannelDelivery,(t.id,item['id'])).lease_until=0
    assert prepare_delivery(t.id,ch['id']) is None
    with transaction(t.id) as s:assert s.get(ChannelDelivery,(t.id,item['id'])).status=='uncertain'

def test_late_acceptance_remains_evidence_without_resetting_state(make_tenant,make_employee,monkeypatch):
    from mvp.service import add_message
    t=make_tenant();c,_=make_employee(t);ch=create(c,monkeypatch,'dingtalk');cid,_=ingest(t.id,ch['id'],ch['version'],frame())
    with transaction(t.id) as s:add_message(s,s.get(Conversation,(t.id,cid)),'ai','测试回答')
    item=prepare_delivery(t.id,ch['id']);c.post('/api/channels/'+ch['id']+'/disable',json={'version':ch['version']})
    complete_delivery(t.id,item)
    with transaction(t.id) as s:
        d=s.get(ChannelDelivery,(t.id,item['id']));assert d.status=='accepted_stale' and d.accepted_at

def test_web_channel_knowledge_scope_applies_to_worker(make_tenant,make_employee,monkeypatch):
    from fastapi.testclient import TestClient
    from mvp.app import app
    from mvp.worker import lease_ai,run_ai
    t=make_tenant();c,_=make_employee(t)
    with transaction(t.id) as s:
        k=Knowledge(tenant_id=t.id,family=uid(),title='仅内部场景公开 FAQ',content='这个测试服务支持预约咨询，具体服务范围由工作人员确认。',keywords=['预约'],status='published',author_id='test');s.add(k);s.flush();kid=k.id
    body=config();body['config']['knowledge_ids']=[kid];ch=c.post('/api/channels',json=body).json()
    c.post('/api/channels/'+ch['id']+'/test',json={'version':ch['version']});c.post('/api/channels/'+ch['id']+'/enable',json={'version':ch['version']})
    v=TestClient(app,base_url='https://testserver');r=v.post('/api/public/session/'+t.slug+'?channel='+ch['id']).json();v.headers.update({'Authorization':'Bearer '+r['token'],'X-CSRF-Token':r['csrf']})
    cid=v.post('/api/public/conversations',json={}).json()['id'];v.post('/api/public/conversations/'+cid+'/messages',json={'text':'企业官网建设','client_id':uid()})
    run_ai(t.id,lease_ai(t.id));answer=v.get('/api/public/conversations/'+cid).json()['messages'][-1]
    assert not any(ref['id']=='faq' for ref in answer['citations'])

def test_notification_signing_options_are_secret(make_tenant,make_employee):
    c,_=make_employee(make_tenant());body={'kind':'dingtalk','webhook':'https://oapi.dingtalk.com/robot/send?access_token=synthetic-notice','signing_secret':'synthetic-signing-key','keyword':'客服提醒'}
    assert c.put('/api/notifications/config',json=body).status_code==200
    r=c.get('/api/settings');assert 'synthetic-signing-key' not in r.text and 'synthetic-notice' not in r.text
    assert r.json()['notification_signed'] and r.json()['notification_keyword']=='客服提醒'

def test_wechat_native_human_stops_robot_ingress_and_sending(make_tenant,make_employee,monkeypatch):
    from mvp.service import add_message
    t=make_tenant();c,_=make_employee(t);ch=create(c,monkeypatch,'wechat_kf')
    with transaction(t.id) as s:s.get(Channel,(t.id,ch['id'])).enabled=True
    msg={'msgid':uid(),'msgtype':'text','text':{'content':'你好'},'origin':3,'external_userid':'synthetic-wechat','open_kfid':ch['config']['open_kf_id'],'_native_human':True}
    cid,_=ingest(t.id,ch['id'],ch['version'],msg)
    with transaction(t.id) as s:
        convo=s.get(Conversation,(t.id,cid));assert convo.state=='HUMAN_ACTIVE';assert not s.scalar(rows(s,AIJob,t.id))
        add_message(s,convo,'ai','不应发送')
    assert prepare_delivery(t.id,ch['id']) is None

@pytest.mark.parametrize('origin',['http://merchant.example:bad','javascript://merchant.example','https://user:password@merchant.example','https://merchant.example/path'])
def test_invalid_origins_cannot_enable_sdk(make_tenant,make_employee,origin):
    c,_=make_employee(make_tenant());body=config();body['config']['allowed_origins']=[origin]
    assert c.post('/api/channels',json=body).status_code==422

def test_channel_unbinding_discards_credentials_and_frees_identity(make_tenant,make_employee,monkeypatch):
    t=make_tenant();c,_=make_employee(t);ch=create(c,monkeypatch,'dingtalk')
    paused=c.post('/api/channels/'+ch['id']+'/disable',json={'version':ch['version']}).json()
    result=c.post('/api/channels/'+ch['id']+'/unbind',json={'version':paused['version']});assert result.status_code==200
    with transaction(t.id) as s:
        saved=s.get(Channel,(t.id,ch['id']));assert not saved.cipher and saved.identity_key is None and not saved.enabled

def test_dingtalk_stream_acknowledges_only_after_ingress(monkeypatch):
    import asyncio
    from mvp import channel_gateway as gw
    calls=[];messages=[frame(),{'type':'SYSTEM','headers':{'topic':'disconnect','messageId':'end'},'data':'{}'}]
    class Socket:
        sent=[]
        async def __aenter__(self):return self
        async def __aexit__(self,*_):return False
        async def send(self,value):self.sent.append(json.loads(value))
        async def recv(self):return json.dumps(messages.pop(0))
    socket=Socket();monkeypatch.setattr(gw,'connect',lambda *a,**k:socket);monkeypatch.setattr(gw,'dingtalk_connection',lambda *a:'wss://example.invalid')
    monkeypatch.setattr(gw,'renew',lambda *a:True);monkeypatch.setattr(gw,'deliver_http',lambda *a:None)
    monkeypatch.setattr(gw,'ingest',lambda *a:calls.append('persisted'));monkeypatch.setattr(gw,'STOP',False)
    asyncio.run(gw.run_dingtalk({'tenant':'synthetic','id':'synthetic','version':1,'config':{},'credentials':{}}))
    assert calls==['persisted'] and socket.sent[0]['code']==200 and socket.sent[-1]['headers']['messageId']=='end'

def test_wecom_stream_authenticates_then_routes_callback(monkeypatch):
    import asyncio
    from mvp import channel_gateway as gw
    calls=[]
    class Finished(Exception):pass
    class Socket:
        def __init__(self):self.sent=[];self.n=0
        async def __aenter__(self):return self
        async def __aexit__(self,*_):return False
        async def send(self,value):self.sent.append(json.loads(value))
        async def recv(self):
            self.n+=1
            if self.n==1:return json.dumps({'headers':self.sent[0]['headers'],'errcode':0})
            if self.n==2:return json.dumps({'cmd':'aibot_msg_callback','headers':{'req_id':'synthetic'},'body':{'msgtype':'text'}})
            raise Finished()
    socket=Socket();monkeypatch.setattr(gw,'connect',lambda *a,**k:socket);monkeypatch.setattr(gw,'renew',lambda *a:True)
    monkeypatch.setattr(gw,'prepare_delivery',lambda *a:None);monkeypatch.setattr(gw,'ingest',lambda *a:calls.append('persisted'));monkeypatch.setattr(gw,'STOP',False)
    with pytest.raises(Finished):asyncio.run(gw.run_wecom({'tenant':'synthetic','id':'synthetic','version':1,'config':{'bot_id':'synthetic'},'credentials':{'secret':'synthetic'}}))
    assert socket.sent[0]['cmd']=='aibot_subscribe' and calls==['persisted']

def test_dingtalk_notification_signing_and_keyword(monkeypatch):
    import urllib.parse,hmac
    from mvp.notifications import send_notification
    from server.model_gateway import protect
    captured={}
    class Response:
        status_code=200
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def iter_bytes(self):yield b'{"errcode":0}'
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def stream(self,method,url,json):captured.update(url=url,payload=json);return Response()
    monkeypatch.setattr('mvp.notifications.httpx.Client',Client);monkeypatch.setattr('mvp.notifications.time.time',lambda:1234)
    options=protect(json.dumps({'signing_secret':'synthetic-sign','keyword':'客服提醒'}))
    assert send_notification('dingtalk',protect('https://oapi.dingtalk.com/robot/send?access_token=synthetic'),'有新任务',options)==(True,'')
    query=urllib.parse.parse_qs(urllib.parse.urlsplit(captured['url']).query)
    expected=base64.b64encode(hmac.new(b'synthetic-sign',b'1234000\nsynthetic-sign',hashlib.sha256).digest()).decode()
    assert query['sign']==[expected] and query['timestamp']==['1234000']
    assert captured['payload']['text']['content'].startswith('SalesCare：客服提醒：')

def test_external_stop_clears_delivery_routes(make_tenant,make_employee,monkeypatch):
    t=make_tenant();c,_=make_employee(t);ch=create(c,monkeypatch,'dingtalk');cid,_=ingest(t.id,ch['id'],ch['version'],frame())
    cid2,_=ingest(t.id,ch['id'],ch['version'],frame(text='停止联系'));assert cid2==cid
    with transaction(t.id) as s:
        thread=s.scalar(rows(s,ExternalThread,t.id));assert not thread.route_cipher and thread.eligible_until==0
        assert s.get(Conversation,(t.id,cid)).state=='CLOSED'

def test_database_rejects_cross_tenant_channel_links(make_tenant,make_employee,monkeypatch):
    from sqlalchemy.exc import IntegrityError
    t=make_tenant();other=make_tenant();c,_=make_employee(t);ch=create(c,monkeypatch)
    with pytest.raises(IntegrityError):
        with transaction(other.id) as s:s.add(Conversation(tenant_id=other.id,visitor_id=uid(),channel_id=ch['id']));s.flush()
