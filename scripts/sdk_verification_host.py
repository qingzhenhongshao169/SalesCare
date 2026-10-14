"""Loopback-only independent merchant website using the real v1 JS SDK."""
import html,json
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
@app.get('/')
def host():
    info=json.loads((Path(__file__).resolve().parents[1]/'.mvp-runtime/sdk-qa-channel.json').read_text('utf-8'))
    channel=html.escape(info['channel'],quote=True)
    return HTMLResponse('''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>SalesCare JS SDK 独立网站验收</title>
<style>body{font:17px system-ui;background:#f2f6f5;color:#25413d;padding:44px}h1{font-size:30px}p{max-width:570px;line-height:1.8}button{padding:12px;border:1px solid #c9dcd4;border-radius:8px;background:white;cursor:pointer}</style>
<h1>企业网站 · JS SDK 验收</h1><p>此网站与客服服务使用不同端口。仅使用合成资料，测试配置加载、打开关闭、重复初始化和会话恢复。</p><p id="ready">正在加载 SDK…</p>
<button onclick="window.SalesCare.init({company:'ui-verification',channel:'CHANNEL'}).open()">通过 SDK 打开咨询</button>
<button onclick="window.SalesCare.init({company:'ui-verification',channel:'CHANNEL'}).close()">通过 SDK 收起咨询</button>
<button onclick="window.SalesCare.init({company:'ui-verification',channel:'CHANNEL'});document.getElementById('repeat').textContent='已再次初始化；咨询入口应仍只有一个'">再次初始化</button><p id="repeat"></p>
<script>window.addEventListener('salescare:ready',()=>document.getElementById('ready').textContent='SDK 已就绪 · 已收到渠道配置');window.addEventListener('salescare:error',()=>document.getElementById('ready').textContent='SDK 加载失败，请检查配置');</script>
<script src="http://127.0.0.1:8768/sdk/v1/salescare.js" data-company="ui-verification" data-channel="CHANNEL" defer></script></html>'''.replace('CHANNEL',channel))
