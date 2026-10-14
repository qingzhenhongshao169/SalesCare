"""Bounded official platform APIs; no provider bodies, secrets or payload logging."""
import asyncio
import base64
import hashlib
import hmac
import json
import struct
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit,urlencode
import httpx
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes

class PlatformError(Exception):
    def __init__(self,code):self.code=code;super().__init__(code)

def platform_url(url,host,path=None):
    p=urlsplit(url)
    if p.scheme!='https' or p.hostname!=host or p.port not in (None,443) or p.username or p.password or p.fragment or (path and p.path!=path):raise PlatformError('invalid_response')
    return url

def api_request(url,method='POST',payload=None,headers=None):
    try:
        with httpx.Client(timeout=12,trust_env=False,follow_redirects=False) as client:
            with client.stream(method,url,json=payload,headers=headers) as r:
                if r.status_code in (401,403):raise PlatformError('auth')
                if r.status_code==429:raise PlatformError('rate_limited')
                if r.status_code!=200:raise PlatformError('provider')
                data=bytearray()
                for part in r.iter_bytes():
                    data.extend(part)
                    if len(data)>250000:raise PlatformError('invalid_response')
                value=json.loads(data)
                if not isinstance(value,dict):raise PlatformError('invalid_response')
                if value.get('errcode',0)!=0:
                    raise PlatformError('auth' if value.get('errcode') in (40001,40013,40014,42001,41001,48002) else 'rate_limited' if value.get('errcode') in (45009,45011) else 'provider')
                return value
    except httpx.ConnectTimeout:raise PlatformError('connect_failed') from None
    except httpx.TimeoutException:raise PlatformError('timeout') from None
    except httpx.ConnectError:raise PlatformError('connect_failed') from None
    except httpx.HTTPError:raise PlatformError('network') from None
    except (ValueError,TypeError):raise PlatformError('invalid_response') from None

def dingtalk_connection(config,secret):
    result=api_request('https://api.dingtalk.com/v1.0/gateway/connections/open',payload={'clientId':config['client_id'],'clientSecret':secret['client_secret'],'subscriptions':[{'type':'CALLBACK','topic':'/v1.0/im/bot/messages/get'}],'ua':'SalesCare/1','localIp':'127.0.0.1'})
    p=urlsplit(result.get('endpoint',''))
    if p.scheme!='wss' or not p.hostname or not (p.hostname=='dingtalk.com' or p.hostname.endswith('.dingtalk.com')) or p.port not in (None,443) or p.username or p.password or p.query or p.fragment or not isinstance(result.get('ticket'),str):raise PlatformError('invalid_response')
    return result['endpoint']+'?'+urlencode({'ticket':result['ticket']})

_TOKENS={}
def wechat_token(config,secret):
    identity=hashlib.sha256((config['corp_id']+secret['secret']).encode()).hexdigest()
    cached=_TOKENS.get(identity)
    if cached and cached[1]>time.time():return cached[0]
    result=api_request('https://qyapi.weixin.qq.com/cgi-bin/gettoken?'+urlencode({'corpid':config['corp_id'],'corpsecret':secret['secret']}),'GET')
    token=result.get('access_token')
    if not isinstance(token,str) or not token:raise PlatformError('invalid_response')
    if len(_TOKENS)>100:_TOKENS.clear()
    _TOKENS[identity]=(token,time.time()+min(7000,int(result.get('expires_in',7200)))-120)
    return token

def wechat_api(path,config,secret,payload):
    return api_request('https://qyapi.weixin.qq.com/cgi-bin/kf/'+path+'?'+urlencode({'access_token':wechat_token(config,secret)}),payload=payload)

async def wecom_probe(config,secret):
    from websockets.asyncio.client import connect
    async with connect('wss://openws.work.weixin.qq.com',open_timeout=10,close_timeout=2,max_size=250000,proxy=None) as ws:
        key='probe-'+str(time.time_ns())
        await ws.send(json.dumps({'cmd':'aibot_subscribe','headers':{'req_id':key},'body':{'bot_id':config['bot_id'],'secret':secret['secret']}}))
        while True:
            frame=json.loads(await asyncio.wait_for(ws.recv(),10))
            if frame.get('headers',{}).get('req_id')==key:
                if frame.get('errcode',0)!=0:raise PlatformError('auth')
                return True

def check_credentials(kind,config,secret):
    try:
        if kind=='web':return True,''
        if kind=='dingtalk':dingtalk_connection(config,secret)
        elif kind=='wecom_bot':asyncio.run(wecom_probe(config,secret))
        elif kind=='wechat_kf':
            result=wechat_api('account/list',config,secret,{})
            if not any(k.get('open_kfid')==config['open_kf_id'] for k in result.get('account_list',[])):raise PlatformError('auth')
        else:raise PlatformError('invalid_response')
        return True,''
    except PlatformError as e:return False,e.code
    except (TimeoutError,asyncio.TimeoutError):return False,'timeout'
    except Exception:return False,'network'

def send_dingtalk(config,secret,route,text):
    url=platform_url(route.get('session_webhook',''),'oapi.dingtalk.com','/robot/sendBySession')
    if route.get('expires_at',0)<=time.time():raise PlatformError('window_expired')
    result=api_request(url,payload={'msgtype':'text','text':{'content':text},'at':{'atUserIds':[route['sender_id']],'isAtAll':False}})
    if 'errcode' not in result:raise PlatformError('invalid_response')
    return True

def send_wechat(config,secret,route,text,message_id):
    state=wechat_api('service_state/get',config,secret,{'external_userid':route['sender_id'],'open_kfid':config['open_kf_id']})
    if state.get('service_state') not in (0,1,2,3,4):raise PlatformError('invalid_response')
    if state.get('service_state')==3:raise PlatformError('native_human')
    if state.get('service_state')==4:raise PlatformError('window_expired')
    result=wechat_api('send_msg',config,secret,{'touser':route['sender_id'],'open_kfid':config['open_kf_id'],'msgid':message_id,'msgtype':'text','text':{'content':text}})
    if 'errcode' not in result:raise PlatformError('invalid_response')
    return True

def parse_xml(raw):
    if len(raw)>64000 or b'<!' in raw:raise PlatformError('invalid_response')
    try:return {child.tag:child.text or '' for child in ET.fromstring(raw)}
    except ET.ParseError:raise PlatformError('invalid_response') from None

def decrypt_callback(secret,corp_id,encrypted,signature,timestamp,nonce):
    try:
        if abs(time.time()-int(timestamp))>300:raise PlatformError('auth')
        expected=hashlib.sha1(''.join(sorted([secret['token'],timestamp,nonce,encrypted])).encode()).hexdigest()
        if not hmac.compare_digest(expected,signature):raise PlatformError('auth')
        key=base64.b64decode(secret['encoding_aes_key']+'=')
        raw=base64.b64decode(encrypted,validate=True)
        decoder=Cipher(algorithms.AES(key),modes.CBC(key[:16])).decryptor();plain=decoder.update(raw)+decoder.finalize()
        padding=plain[-1]
        if padding<1 or padding>32 or plain[-padding:]!=bytes([padding])*padding:raise PlatformError('auth')
        plain=plain[:-padding];size=struct.unpack('!I',plain[16:20])[0]
        if size>64000 or len(plain)<20+size or plain[20+size:].decode()!=corp_id:raise PlatformError('auth')
        return plain[20:20+size]
    except PlatformError:raise
    except Exception:raise PlatformError('auth') from None
