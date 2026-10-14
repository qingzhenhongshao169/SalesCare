"""Supervised, leased platform connections. Credentials never enter argv or logs."""
import asyncio
import json
import signal
import time
from websockets.asyncio.client import connect
from sqlalchemy import select
from .db import transaction,rows,locked,database_time
from .models import Channel,WorkerState,Tenant,ChannelDelivery,uid
from .channels import credentials
from .channel_messages import ingest,prepare_delivery,complete_delivery,deliver_http,check_delivery
from .channel_transport import dingtalk_connection,wechat_api,PlatformError
from server.model_gateway import protect

OWNER=uid()
STOP=False

def acquire(tid,cid):
    with transaction(tid) as s:
        ch=s.scalar(rows(s,Channel,tid).where(Channel.id==cid).with_for_update(skip_locked=True))
        now=database_time(s)
        if not ch or not ch.enabled or ch.kind=='web' or ch.lease_until>now or ch.tested_version!=ch.version:return None
        ch.lease_owner=OWNER;ch.lease_until=now+45;ch.status='connecting';ch.diagnostic=''
        return {'tenant':tid,'id':cid,'kind':ch.kind,'version':ch.version,'config':dict(ch.config),'credentials':credentials(ch)}

def renew(item,status='connected',error=''):
    with transaction(item['tenant']) as s:
        ch=locked(s,Channel,item['tenant'],item['id']);now=database_time(s)
        if not ch or not ch.enabled or ch.version!=item['version'] or ch.lease_owner!=OWNER or ch.lease_until<=now:return False
        ch.lease_until=now+45;ch.heartbeat_at=now;ch.status=status;ch.diagnostic=error
        return True

def release(item,error):
    with transaction(item['tenant']) as s:
        ch=locked(s,Channel,item['tenant'],item['id'])
        if ch and ch.version==item['version'] and ch.lease_owner==OWNER:
            ch.lease_until=database_time(s)+min(60,10 if error=='network' else 30);ch.status='disconnected';ch.diagnostic=error

def heartbeat():
    with transaction() as s:
        row=s.get(WorkerState,'channel-gateway')
        if not row:s.add(WorkerState(id='channel-gateway',heartbeat_at=time.time()))
        else:row.heartbeat_at=time.time()

async def run_dingtalk(item):
    url=await asyncio.to_thread(dingtalk_connection,item['config'],item['credentials'])
    async with connect(url,open_timeout=12,close_timeout=2,ping_interval=20,ping_timeout=20,max_size=250000,proxy=None) as ws:
        if not renew(item):return
        last=0
        while not STOP:
            if time.monotonic()-last>10:
                if not renew(item):return
                last=time.monotonic()
            await asyncio.to_thread(deliver_http,item['tenant'],item['id'])
            try:frame=json.loads(await asyncio.wait_for(ws.recv(),.5))
            except TimeoutError:continue
            code=200;message='OK'
            try:
                if frame.get('type')=='CALLBACK' and frame.get('headers',{}).get('topic')=='/v1.0/im/bot/messages/get':
                    ingest(item['tenant'],item['id'],item['version'],frame,OWNER)
            except PlatformError as e:
                if e.code=='unsupported_message':
                    with transaction(item['tenant']) as s:locked(s,Channel,item['tenant'],item['id']).diagnostic=e.code
                elif e.code=='channel_disabled':return
                else:code=500;message='RETRY'
            except Exception:code=500;message='RETRY'
            ack={'code':code,'headers':{'messageId':frame.get('headers',{}).get('messageId',''),'contentType':'application/json'},'message':message,'data':frame.get('data',{}) if frame.get('type')=='SYSTEM' else ''}
            await ws.send(json.dumps(ack))
            if frame.get('type')=='SYSTEM' and frame.get('headers',{}).get('topic')=='disconnect':return

async def run_wecom(item):
    inflight={};last=0;ping=0
    async with connect('wss://openws.work.weixin.qq.com',open_timeout=12,close_timeout=2,ping_interval=None,max_size=250000,proxy=None) as ws:
        key=uid()
        await ws.send(json.dumps({'cmd':'aibot_subscribe','headers':{'req_id':key},'body':{'bot_id':item['config']['bot_id'],'secret':item['credentials']['secret']}}))
        while True:
            frame=json.loads(await asyncio.wait_for(ws.recv(),12))
            if frame.get('headers',{}).get('req_id')==key:
                if frame.get('errcode',0)!=0:raise PlatformError('auth')
                break
        while not STOP:
            now=time.monotonic()
            if now-last>10:
                if not renew(item):return
                last=now
            if now-ping>25:
                await ws.send(json.dumps({'cmd':'ping','headers':{'req_id':'ping-'+uid()}}));ping=now
            for key,(delivery,started) in list(inflight.items()):
                if now-started>12:complete_delivery(item['tenant'],delivery,'uncertain');inflight.pop(key)
            if not inflight:
                delivery=prepare_delivery(item['tenant'],item['id'])
                if delivery:
                    with transaction(item['tenant']) as s:check_delivery(s,s.get(ChannelDelivery,(item['tenant'],delivery['id'])))
                    key=uid();inflight[key]=(delivery,now)
                    await ws.send(json.dumps({'cmd':'aibot_send_msg','headers':{'req_id':key},'body':{'chatid':delivery['route']['room_id'],'msgtype':'markdown','markdown':{'content':delivery['text']}}}))
            try:frame=json.loads(await asyncio.wait_for(ws.recv(),.5))
            except TimeoutError:continue
            key=frame.get('headers',{}).get('req_id')
            if key in inflight:
                delivery,_=inflight.pop(key)
                complete_delivery(item['tenant'],delivery,'' if frame.get('errcode')==0 else 'provider')
            elif frame.get('cmd')=='aibot_msg_callback':
                try:ingest(item['tenant'],item['id'],item['version'],frame,OWNER)
                except PlatformError as e:
                    if e.code=='channel_disabled':return
                    if e.code=='unsupported_message':
                        with transaction(item['tenant']) as s:locked(s,Channel,item['tenant'],item['id']).diagnostic=e.code
                    else:raise

def poll_wechat(item):
    with transaction(item['tenant']) as s:
        ch=locked(s,Channel,item['tenant'],item['id'])
        if ch.poll_due>time.time():return
        state=json.loads(protect(ch.runtime_cipher,decrypt=True)) if ch.runtime_cipher else {}
        ch.poll_due=time.time()+30
    result=wechat_api('sync_msg',item['config'],item['credentials'],{'cursor':state.get('cursor',''),'token':state.get('sync_token',''),'open_kfid':item['config']['open_kf_id'],'limit':100,'voice_format':0})
    for message in result.get('msg_list',[]):
        try:
            if message.get('origin')==3 and message.get('external_userid'):
                native=wechat_api('service_state/get',item['config'],item['credentials'],{'open_kfid':item['config']['open_kf_id'],'external_userid':message['external_userid']})
                message['_native_human']=native.get('service_state')==3
            ingest(item['tenant'],item['id'],item['version'],message,OWNER)
        except PlatformError as e:
            if e.code!='unsupported_message':raise
    with transaction(item['tenant']) as s:
        ch=locked(s,Channel,item['tenant'],item['id'])
        if ch.version==item['version'] and ch.lease_owner==OWNER:
            # Preserve a callback arriving during the network round trip.
            current=json.loads(protect(ch.runtime_cipher,decrypt=True)) if ch.runtime_cipher else {}
            current['cursor']=result.get('next_cursor',state.get('cursor',''));ch.runtime_cipher=protect(json.dumps(current))
            if result.get('has_more'):ch.poll_due=0

async def run_wechat(item):
    while not STOP:
        if not renew(item):return
        await asyncio.to_thread(poll_wechat,item)
        await asyncio.to_thread(deliver_http,item['tenant'],item['id'])
        await asyncio.sleep(1)

async def run(item):
    error='network'
    try:
        await {'dingtalk':run_dingtalk,'wecom_bot':run_wecom,'wechat_kf':run_wechat}[item['kind']](item)
    except PlatformError as e:error=e.code
    except asyncio.CancelledError:raise
    except Exception:pass
    finally:release(item,error)

async def main_async():
    active={}
    try:
        while not STOP:
            heartbeat()
            for key,task in list(active.items()):
                if task.done():
                    try:task.result()
                    except Exception:pass
                    active.pop(key)
            with transaction() as s:tids=list(s.scalars(select(Tenant.id).where(Tenant.active==True)))
            for tid in tids:
                with transaction(tid) as s:ids=list(s.scalars(rows(s,Channel,tid).where(Channel.enabled==True,Channel.kind!='web').with_only_columns(Channel.id)))
                for cid in ids:
                    if (tid,cid) not in active and len(active)<100:
                        item=acquire(tid,cid)
                        if item:active[(tid,cid)]=asyncio.create_task(run(item))
            await asyncio.sleep(3)
    finally:
        for task in active.values():task.cancel()
        await asyncio.gather(*active.values(),return_exceptions=True)

def halt(*_):
    global STOP
    STOP=True

if __name__=='__main__':
    signal.signal(signal.SIGINT,halt);signal.signal(signal.SIGTERM,halt)
    asyncio.run(main_async())
