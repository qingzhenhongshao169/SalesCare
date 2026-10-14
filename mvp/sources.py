import hashlib
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit, urlunsplit
import httpx
from bs4 import BeautifulSoup
from fastapi import HTTPException


def fetch_public(url, allowed_hosts):
    p=urlsplit(url)
    if p.scheme!='https' or p.username or p.password or p.query or p.fragment or p.port not in (None,443) or p.hostname not in allowed_hosts:
        raise HTTPException(422,'资料地址必须是白名单内的 HTTPS 页面，不包含账号或查询参数。')
    try:
        addresses={ipaddress.ip_address(a[4][0]) for a in socket.getaddrinfo(p.hostname,443,type=socket.SOCK_STREAM)}
    except OSError: raise HTTPException(422,'来源域名无法解析。') from None
    if not addresses or any(not a.is_global for a in addresses):
        raise HTTPException(422,'来源地址不能指向内网或保留网段。')
    # Pin the connection IP while retaining TLS certificate/SNI hostname validation.
    address=sorted(addresses,key=lambda x:x.version)[0]
    host='['+str(address)+']' if address.version==6 else str(address)
    target=urlunsplit(('https',host,p.path or '/','',''))
    # Use the operating system's verified trust store as well as Python's defaults.
    # On Windows this includes enterprise-installed root certificates; verification remains mandatory.
    with httpx.Client(verify=ssl.create_default_context(),trust_env=False,follow_redirects=False,timeout=20) as client:
        with client.stream('GET',target,headers={'Host':p.hostname,'User-Agent':'SalesCareKnowledge/1.0'},
                           extensions={'sni_hostname':p.hostname}) as response:
            if response.status_code!=200: raise HTTPException(502,'来源未返回可读取正文；已保留旧知识。')
            if 'text/' not in response.headers.get('content-type',''): raise HTTPException(422,'仅支持网页或文本来源。')
            data=bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data)>500000: raise HTTPException(422,'来源页面超过500KB限制。')
    html=data.decode('utf-8',errors='replace')
    soup=BeautifulSoup(html,'html.parser')
    for tag in soup(['script','style','form','input','textarea','nav','header','footer','noscript']): tag.decompose()
    content='\n'.join(x.strip() for x in (soup.main or soup.body or soup).get_text('\n').splitlines() if x.strip())[:20000]
    if len(content)<30: raise HTTPException(422,'页面正文不足，请用文本导入。')
    return content, hashlib.sha256(content.encode()).hexdigest()
