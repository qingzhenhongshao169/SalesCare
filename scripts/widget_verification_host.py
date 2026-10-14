"""A loopback-only synthetic host exposing one fixture, never a filesystem browser."""
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)

@app.get('/')
def fixture():
    return HTMLResponse('''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>SalesCare 网站嵌入验收</title>
    <style>body{font:18px system-ui;background:#f2f6f5;color:#25413d;padding:50px}h1{font-size:32px}p{max-width:550px;line-height:1.8}</style>
    <h1>企业网站 · 嵌入验收</h1><p>这是独立的网站页面，右下角的咨询按钮通过 Widget 打开企业客服。此环境只使用合成验收资料。</p>
    <script src="http://127.0.0.1:8768/widget.js" data-company="ui-verification" defer></script></html>''')
