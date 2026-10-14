"""Official employee reminder adapters. Fixed errors only; never return provider bodies."""
from urllib.parse import urlsplit,urlencode
import base64,hashlib,hmac,json,time
import httpx
from server import model_gateway

def send_notification(kind,cipher,text,options_cipher=''):
    try:
        url=model_gateway.protect(cipher,decrypt=True)
        p=urlsplit(url);expected={'wecom':'qyapi.weixin.qq.com','dingtalk':'oapi.dingtalk.com'}.get(kind)
        if not expected or p.scheme!='https' or p.hostname!=expected or p.username or p.password or p.port not in (None,443):
            return False,'invalid_destination'
        options=json.loads(model_gateway.protect(options_cipher,decrypt=True)) if options_cipher else {}
        if kind=='dingtalk' and options.get('signing_secret'):
            timestamp=str(int(time.time()*1000));secret=options['signing_secret']
            sign=base64.b64encode(hmac.new(secret.encode(),(timestamp+'\n'+secret).encode(),hashlib.sha256).digest()).decode()
            url+=('&' if p.query else '?')+urlencode({'timestamp':timestamp,'sign':sign})
        if options.get('keyword'):text=options['keyword']+'：'+text
        with httpx.Client(trust_env=False,follow_redirects=False,timeout=10) as client:
            with client.stream('POST',url,json={'msgtype':'text','text':{'content':'SalesCare：'+text}}) as response:
                if response.status_code==429:return False,'rate_limited'
                if response.status_code!=200:return False,'provider_http'
                raw=bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw)>64000:return False,'invalid_response'
                payload=__import__('json').loads(raw)
                if not isinstance(payload,dict) or payload.get('errcode')!=0:return False,'provider_rejected'
        return True,''
    except httpx.TimeoutException:return False,'timeout'
    except (httpx.HTTPError,ValueError,KeyError,TypeError):return False,'network_or_format'
    except Exception:return False,'configuration_error'
