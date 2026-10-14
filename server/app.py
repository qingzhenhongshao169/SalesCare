import copy
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .domain import event, make_conversation, make_task, metrics, now, profile, reply, seed, tick, uid
from . import model_gateway
from .retrieval import retrieve, citation, eligible

ROOT = Path(__file__).resolve().parents[1]
DB = Path(os.environ.get("SALESCARE_DB", ROOT / "runtime" / "demo.sqlite3"))
sessions = {}
db_lock = threading.RLock()
PERSONAS = {"admin": {"name": "苏晴", "role": "admin"}, "lin": {"name": "林知夏", "role": "agent"}, "zhou": {"name": "周予安", "role": "agent"}, "editor": {"name": "陈言", "role": "editor"}, "publisher": {"name": "沈一", "role": "publisher"}, "visitor": {"name": "演示访客", "role": "visitor"}}
TENANTS = ("biaofun", "yunyu", "hexu")


def initialize():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS tenant_state (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS demo_session (token_hash TEXT PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL)")
        conn.execute("DELETE FROM demo_session WHERE expires < ?", (time.time(),))
        for tenant in TENANTS:
            if conn.execute("SELECT 1 FROM tenant_state WHERE id=?", (tenant,)).fetchone() is None:
                conn.execute("INSERT INTO tenant_state VALUES (?,?)", (tenant, json.dumps(seed(tenant), ensure_ascii=False)))
            else:
                row=conn.execute("SELECT document FROM tenant_state WHERE id=?",(tenant,)).fetchone()
                d=json.loads(row[0]);changed=False
                for c in d["conversations"]:
                    if c.pop("reply_pending",None):
                        changed=True
                        if c["state"] in ("AI_ACTIVE","HANDOFF_PENDING"):
                            last=next((m["text"] for m in reversed(c["messages"]) if m["role"]=="visitor"),"")
                            answer=reply(d,c,last);answer["model_fallback"]=True
                            c["messages"].append(answer)
                if changed: conn.execute("UPDATE tenant_state SET document=? WHERE id=?",(json.dumps(d,ensure_ascii=False),tenant))
    model_gateway.initialize(DB)


@contextmanager
def state(tenant):
    with db_lock, sqlite3.connect(DB, timeout=15) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT document FROM tenant_state WHERE id=?", (tenant,)).fetchone()
        if row is None:
            raise HTTPException(404, "企业不存在")
        d = json.loads(row[0])
        tick(d)
        yield d
        conn.execute("UPDATE tenant_state SET document=? WHERE id=?", (json.dumps(d, ensure_ascii=False), tenant))


@asynccontextmanager
async def lifespan(app):
    initialize()
    stop = threading.Event()
    def worker():
        while not stop.wait(1):
            for tenant in TENANTS:
                try:
                    with state(tenant):
                        pass
                except sqlite3.Error:
                    import logging
                    logging.exception("Demo SLA worker failed")
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    yield
    stop.set()
    thread.join(timeout=3)


app = FastAPI(title="SalesCare local demo", version="0.2.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Do not echo input values (especially API credentials) in validation responses.
    return JSONResponse(status_code=422,content={"detail":"输入格式不正确，请检查字段长度与数值范围", "fields":[list(e["loc"]) for e in exc.errors()]})


def auth(request: Request):
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    s = sessions.get(token)
    if not s and token:
        with sqlite3.connect(DB) as conn:
            row = conn.execute("SELECT payload FROM demo_session WHERE token_hash=? AND expires>?", (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
            if row:
                s = json.loads(row[0])
                sessions[token] = s
    if not s or s["expires"] < time.time():
        raise HTTPException(401, "演示会话已失效，请重新选择身份")
    return s


def require(s, *roles):
    if s["role"] not in roles:
        raise HTTPException(403, "当前身份没有此操作权限")


def find(d, collection, resource_id):
    item = next((x for x in d[collection] if x["id"] == resource_id), None)
    if item is None:
        raise HTTPException(404, "记录不存在或无权访问")
    return item


def conversation(d, s, cid):
    require(s, "visitor", "agent", "admin")
    c = find(d, "conversations", cid)
    if s["role"] == "visitor" and c["visitor_id"] != s["identity"]:
        raise HTTPException(404, "会话不存在或无权访问")
    return c


class Login(BaseModel):
    tenant: str
    persona: str


@app.get("/api/health")
def health():
    return {"ok": True, "mode": "local-demo", "storage": "sqlite", "model": "tenant-configurable-knowledge-and-llm"}


@app.get("/api/demo/options")
def options():
    return {"tenants": [{"id":"biaofun","name":"标梵互动","industry":"数字营销与软件研发"},{"id": "yunyu", "name": "云屿科技", "industry": "协作软件"}, {"id": "hexu", "name": "禾序咨询", "industry": "企业咨询"}], "personas": [{"id": k, **v} for k, v in PERSONAS.items()]}


@app.post("/api/demo/session")
def login(body: Login):
    if body.tenant not in TENANTS or body.persona not in PERSONAS:
        raise HTTPException(400, "无效演示身份")
    token = secrets.token_urlsafe(32)
    s = {**PERSONAS[body.persona], "tenant": body.tenant, "persona": body.persona, "identity": uid("visitor") if body.persona == "visitor" else body.persona, "expires": time.time() + 86400}
    sessions[token] = s
    with sqlite3.connect(DB) as conn:
        conn.execute("INSERT INTO demo_session VALUES (?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), json.dumps(s), s["expires"]))
    return {"token": token, "session": s}


@app.get("/api/bootstrap")
def bootstrap(s=Depends(auth)):
    with state(s["tenant"]) as d:
        for c in d["conversations"]:
            profile(d, c, "")  # Re-validate recommendations against current published catalogue.
        out = copy.deepcopy(d)
        out.update({"server_now": now(d), "metrics": metrics(d), "session": s})
        cfg = model_gateway.load(DB, s["tenant"])
        out["ai_mode"] = "llm" if cfg.get("enabled") and cfg.get("has_key") else "knowledge-retrieval" if s["tenant"]=="biaofun" else "deterministic"
        if s["role"] == "visitor":
            out["conversations"] = [c for c in out["conversations"] if c["visitor_id"] == s["identity"]]
            ids = {c["id"] for c in out["conversations"]}
            out["tasks"] = [t for t in out["tasks"] if t["conversation_id"] in ids]
            out["knowledge"] = []
            out["gaps"] = []
            out["audit"] = []
            out["metrics"] = {}
            out["members"] = []
        elif s["role"] in ("editor", "publisher"):
            out["conversations"] = []
            out["tasks"] = []
            out["audit"] = [a for a in out["audit"] if a["action"].startswith("knowledge.")]
        else:
            for c in out["conversations"]:
                task = next((t for t in out["tasks"] if t["conversation_id"] == c["id"]), None)
                if c["lead"] and not (s["role"] == "agent" and task and task["owner"] == s["persona"] and task["claimed"]):
                    c["lead"]["phone"] = "已保护"
            for t in out["tasks"]:
                if not (s["role"] == "agent" and t["owner"] == s["persona"] and t["claimed"]):
                    t["phone"] = "已保护"
        return out


class Start(BaseModel):
    channel: str = "web"


@app.post("/api/conversations")
def start(body: Start, s=Depends(auth)):
    require(s, "visitor")
    with state(s["tenant"]) as d:
        channel = find(d, "channels", body.channel)
        if not channel["connected"]:
            raise HTTPException(409, "此渠道已停用，请换一个入口")
        c = make_conversation(d, s["identity"], body.channel)
        c["messages"].append({"id": uid("msg"), "role": "ai", "text": f"您好，我是{d['name']}的 AI 接待助手。您可以直接告诉我需求，我会根据企业已确认资料为您解答，也可以随时帮您转接专业顾问。", "at": now(d), "citations": []})
        event(d, "conversation.created", f"{channel['name']} · 新咨询")
        return c


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    client_id: str = Field(min_length=1, max_length=100)


@app.post("/api/conversations/{cid}/messages")
def message(cid: str, body: Message, s=Depends(auth)):
    require(s, "visitor")
    if not body.text.strip():
        raise HTTPException(422, "请输入消息")
    generation = None
    with state(s["tenant"]) as d:
        c = conversation(d, s, cid)
        if c["state"] == "CLOSED":
            raise HTTPException(409, "会话已关闭，请新建咨询")
        if any(m.get("client_id") == body.client_id for m in c["messages"]):
            return c
        if c.get("reply_pending"):
            raise HTTPException(409, "上一条咨询正在处理，请稍候再发送")
        channel = find(d, "channels", c["channel"])
        if not channel["connected"]:
            raise HTTPException(409, "渠道已断开，消息未发送")
        channel["window_open"] = True
        c["messages"].append({"id": uid("msg"), "role": "visitor", "text": body.text.strip(), "at": now(d), "client_id": body.client_id})
        if c["state"] in ("AI_ACTIVE", "HANDOFF_PENDING"):
            cfg = model_gateway.load(DB, s["tenant"])
            if cfg.get("enabled"):
                try: cfg = model_gateway.load(DB, s["tenant"], secret=True)
                except HTTPException: cfg["enabled"] = False
            records = retrieve(d,c["channel"],body.text,now(d)) if cfg.get("enabled") else []
            blocked = any(x in body.text.lower() for x in ["忽略规则","忽略之前","提示词","api密钥","api key","其他企业","其他租户","system prompt"])
            if cfg.get("enabled") and cfg.get("api_key") and records and not blocked:
                profile(d,c,body.text)
                c["reply_pending"] = body.client_id
                history = [{"role":"user" if m["role"]=="visitor" else "assistant", "content":redact_model_text(m["text"])}
                           for m in c["messages"][-9:]]
                while history and history[0]["role"] != "user": history.pop(0)
                generation = (cfg, d["name"], history, copy.deepcopy(records))
            else:
                answer = reply(d, c, body.text)
                if c["state"] == "HANDOFF_PENDING": answer["waiting_for_human"] = True
                c["messages"].append(answer)
        else:
            profile(d, c, body.text)
            t = next((t for t in d["tasks"] if t["conversation_id"] == cid and t["attempted_at"] and t["status"] != "cancelled"), None)
            if t:
                t["reached_at"] = now(d)
                event(d, "contact.reached", f"{t['name']} · 联系后用户有回复（演示）")
        if not generation: return c
    # Never hold the SQLite transaction or SLA worker lock during external model I/O.
    cfg,company,history,records = generation
    try:
        text, used = model_gateway.grounded_answer(cfg,company,history,records)
        result = {"id":uid("msg"),"role":"ai","text":text,"citations":[citation(k) for k in used],"engine":"llm"}
    except (model_gateway.ProviderError, HTTPException):
        result = None
    with state(s["tenant"]) as d:
        c=conversation(d,s,cid)
        if c.get("reply_pending")==body.client_id:
            c.pop("reply_pending",None)
            if c["state"] in ("AI_ACTIVE","HANDOFF_PENDING"):
                active={(k["id"],k["version"],k["content"]) for k in eligible(d,c["channel"],now(d))}
                still_valid=result and all((k["id"],k["version"],k["content"]) in active for k in used)
                current_cfg=model_gateway.load(DB,s["tenant"])
                still_valid=still_valid and current_cfg.get("enabled") and model_gateway.fingerprint(cfg)==model_gateway.fingerprint(current_cfg)
                answer=result if still_valid else reply(d,c,body.text)
                answer["at"]=now(d)
                if not still_valid: answer["model_fallback"]=True
                if c["state"]=="HANDOFF_PENDING": answer["waiting_for_human"]=True
                c["messages"].append(answer)
        return c


def redact_model_text(text):
    text=re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)","[手机号已隐藏]",text)
    return re.sub(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}","[邮箱已隐藏]",text)


@app.get("/api/admin/model")
def model_settings(s=Depends(auth)):
    require(s,"admin")
    return model_gateway.load(DB,s["tenant"])


@app.put("/api/admin/model")
def save_model(body:model_gateway.ModelInput,s=Depends(auth)):
    require(s,"admin")
    result=model_gateway.save(DB,s["tenant"],body)
    with state(s["tenant"]) as d: event(d,"model.configured","模型连接配置已更新（不记录密钥）",s["persona"])
    return result


@app.delete("/api/admin/model/key")
def delete_model_key(s=Depends(auth)):
    require(s,"admin")
    result=model_gateway.delete_secret(DB,s["tenant"])
    with state(s["tenant"]) as d: event(d,"model.key_removed","模型密钥已移除，模型已停用",s["persona"])
    return result


@app.post("/api/admin/model/test")
def test_model(s=Depends(auth)):
    require(s,"admin")
    try: return model_gateway.test_connection(DB,s["tenant"])
    except (model_gateway.ProviderError, HTTPException) as e:
        raise HTTPException(422,str(e) if isinstance(e,model_gateway.ProviderError) else e.detail) from None


class Lead(BaseModel):
    name: str = Field(min_length=1, max_length=30)
    phone: str = Field(default="", max_length=30)
    consent: bool


@app.post("/api/conversations/{cid}/lead")
def lead(cid: str, body: Lead, s=Depends(auth)):
    require(s, "visitor")
    if not body.consent or not body.name.strip():
        raise HTTPException(422, "请填写称呼并同意本次需求联系")
    if body.phone and (len(body.phone) != 11 or not body.phone.isdigit() or not body.phone.startswith("1")):
        raise HTTPException(422, "手机号格式不正确，也可留空使用当前会话联系")
    with state(s["tenant"]) as d:
        c = conversation(d, s, cid)
        if c["state"] == "CLOSED":
            raise HTTPException(409, "会话已关闭，请新建咨询")
        if c.get("lead") and c["lead"].get("consent"):
            return next(t for t in d["tasks"] if t["conversation_id"] == cid and t["status"] != "cancelled")
        ch = find(d, "channels", c["channel"])
        if not ch["connected"] or not ch["window_open"] or not any(m["available"] for m in d["members"]):
            raise HTTPException(409, "当前渠道或值班人员不可用，暂不能接受五分钟联系承诺")
        old = next((t for t in d["tasks"] if t["conversation_id"] == cid and t["status"] == "pending" and t["deadline_at"] is None), None)
        if old:
            stamp = now(d)
            old.update({"consent": True, "name": body.name.strip(), "phone": body.phone, "accepted_at": stamp, "deadline_at": stamp + 300, "sla": "running", "version": old["version"] + 1})
            c["lead"] = {"name": body.name.strip(), "phone": body.phone, "consent": True, "accepted_at": stamp}
            c["name"] = body.name.strip()
            return old
        return make_task(d, c, body.name.strip(), body.phone)


@app.post("/api/conversations/{cid}/handoff")
def handoff(cid: str, s=Depends(auth)):
    require(s, "visitor")
    with state(s["tenant"]) as d:
        c = conversation(d, s, cid)
        if c["state"] == "CLOSED":
            raise HTTPException(409, "会话已关闭，请新建咨询")
        return make_task(d, c, c["name"], consent=False)


@app.post("/api/conversations/{cid}/withdraw")
def withdraw(cid: str, s=Depends(auth)):
    require(s, "visitor")
    with state(s["tenant"]) as d:
        c = conversation(d, s, cid)
        for t in d["tasks"]:
            if t["conversation_id"] == cid and t["status"] != "cancelled":
                t.update({"status": "cancelled", "consent": False, "phone": "", "cancelled_at": now(d), "version": t["version"] + 1})
        if c["lead"]:
            c["lead"].update({"consent": False, "phone": ""})
        c["state"] = "CLOSED"
        event(d, "consent.withdrawn", "用户撤回后续联系授权，停止接待；保留履约记录")
        tick(d)
        return {"ok": True}


class TaskAction(BaseModel):
    version: int
    target: str | None = None
    text: str = Field(default="", max_length=2000)


@app.post("/api/tasks/{tid}/{action}")
def task_action(tid: str, action: str, body: TaskAction, s=Depends(auth)):
    require(s, "agent")
    with state(s["tenant"]) as d:
        t = find(d, "tasks", tid)
        if body.version != t["version"]:
            raise HTTPException(409, "任务已更新，请刷新后重试")
        if t["status"] == "cancelled":
            raise HTTPException(409, "用户已撤回联系授权")
        member = find(d, "members", s["persona"])
        if member["skill"] != t["skill"] or not member["available"]:
            raise HTTPException(403, "当前人员不满足专业技能或值班条件")
        c = find(d, "conversations", t["conversation_id"])
        if action == "claim":
            if t["claimed"] and t["owner"] != s["persona"]:
                raise HTTPException(409, "任务已被其他人员接管")
            t.update({"owner": s["persona"], "claimed": True})
            c["state"] = "HUMAN_ACTIVE"
            event(d, "task.claimed", f"{t['name']} · {s['name']} 接管，AI 已停止自主回复", s["persona"])
        elif action == "transfer":
            if not t["claimed"] or t["owner"] != s["persona"]:
                raise HTTPException(403, "仅当前接管人员可以转交")
            target = find(d, "members", body.target)
            if not target["available"] or target["skill"] != t["skill"] or target["id"] == s["persona"]:
                raise HTTPException(409, "请选择另一位可用的同技能顾问")
            t.update({"owner": target["id"], "claimed": False})
            c["state"] = "HANDOFF_PENDING"
            event(d, "task.transferred", f"{t['name']} · 转交{target['name']}，截止时间不变", s["persona"])
        elif action == "contact":
            if not t["claimed"] or t["owner"] != s["persona"]:
                raise HTTPException(403, "请先以本人身份接管会话")
            if not body.text.strip():
                raise HTTPException(422, "请输入与用户需求相关的联系内容")
            ch = find(d, "channels", t["channel"])
            if not ch["connected"] or not ch["window_open"]:
                raise HTTPException(409, "渠道不可发送，未产生联系证据；原 SLA 继续计时")
            stamp = now(d)
            evidence = {"id": uid("receipt"), "at": stamp, "actor": s["persona"], "type": "local_web_message_accepted" if ch["id"] == "web" else "simulated_channel_accepted", "simulated": True}
            t["evidence"].append(evidence)
            t.update({"attempted_at": t["attempted_at"] or stamp, "status": "contacted"})
            c["messages"].append({"id": uid("msg"), "role": "human", "actor": s["name"], "text": body.text.strip(), "at": stamp})
            event(d, "contact.attempted", f"{t['name']} · 人工消息已受理（演示证据），触达待用户回复", s["persona"])
        else:
            raise HTTPException(404, "操作不存在")
        t["version"] += 1
        tick(d)
        return t


class KnowledgeInput(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=3000)
    keywords: str = Field(min_length=1, max_length=150)
    risk: str = "low"


@app.post("/api/knowledge")
def create_knowledge(body: KnowledgeInput, s=Depends(auth)):
    require(s, "editor", "admin")
    if body.risk not in ("low", "high") or not body.title.strip() or not body.content.strip():
        raise HTTPException(422, "请填写完整知识内容")
    with state(s["tenant"]) as d:
        # High-risk language cannot be down-classified by a form control.
        risk = "high" if body.risk == "high" or any(w in body.content for w in ["元", "价格", "保证", "退款", "承诺", "折扣"]) else "low"
        k = {"id": uid("kb"), "title": body.title.strip(), "content": body.content.strip(), "keywords": [x.strip() for x in body.keywords.split("，") for x in x.split(",") if x.strip()], "risk": risk,
             "status": "draft", "version": 1, "visibility": "public", "channels": ["web", "wechat", "wecom", "dingtalk"], "author": s["persona"], "reviewer": None, "source": "人工提交", "updated_at": now(d), "expires_at": None}
        d["knowledge"].insert(0, k)
        event(d, "knowledge.created", k["title"], s["persona"])
        return k


@app.post("/api/knowledge/{kid}/{action}")
def knowledge_action(kid: str, action: str, s=Depends(auth)):
    with state(s["tenant"]) as d:
        k = find(d, "knowledge", kid)
        if action == "publish":
            require(s, "publisher", "admin")
            if k["risk"] == "high" and k["author"] == s["persona"]:
                raise HTTPException(409, "高风险知识需另一名发布者审核")
            if k["status"] != "draft":
                raise HTTPException(409, "仅候选草稿可以发布")
            if k.get("replaces"):
                old = find(d, "knowledge", k["replaces"])
                if old["status"] != "published":
                    raise HTTPException(409, "原版本已变化，请重新核对")
                old["status"] = "superseded"
            k.update({"status": "published", "reviewer": s["persona"], "updated_at": now(d)})
        elif action == "revoke":
            require(s, "publisher", "admin")
            if k["status"] != "published":
                raise HTTPException(409, "该知识不是已发布状态")
            k["status"] = "revoked"
        elif action == "sync":
            require(s, "editor", "admin")
            if k["status"] != "published":
                raise HTTPException(409, "请选择已发布版本模拟源更新")
            if any(x.get("replaces") == kid and x["status"] == "draft" for x in d["knowledge"]):
                raise HTTPException(409, "该版本已有待审候选")
            candidate = {**copy.deepcopy(k), "id": uid("kb"), "version": k["version"] + 1, "status": "draft", "author": s["persona"], "reviewer": None,
                         "content": k["content"] + "\n更新说明：顾问会在联系前核对您的具体使用需求。", "replaces": kid, "family": k.get("family", k["id"]), "source": "模拟白名单资料更新", "updated_at": now(d)}
            d["knowledge"].insert(0, candidate)
        else:
            raise HTTPException(404, "操作不存在")
        event(d, "knowledge." + action, k["title"], s["persona"])
        return {"ok": True}


class ChannelInput(BaseModel):
    connected: bool | None = None
    window_open: bool | None = None


@app.patch("/api/channels/{chid}")
def channel_update(chid: str, body: ChannelInput, s=Depends(auth)):
    require(s, "admin")
    with state(s["tenant"]) as d:
        ch = find(d, "channels", chid)
        ch.update(body.model_dump(exclude_none=True))
        event(d, "channel.updated", f"{ch['name']} · 连接/发送资格已变更", s["persona"])
        return ch


class Availability(BaseModel):
    available: bool


@app.patch("/api/members/{mid}")
def member_update(mid: str, body: Availability, s=Depends(auth)):
    require(s, "admin")
    with state(s["tenant"]) as d:
        member = find(d, "members", mid)
        member["available"] = body.available
        event(d, "member.updated", f"{member['name']} · {'值班' if body.available else '离线'}", s["persona"])
        return member


class ClockInput(BaseModel):
    seconds: int = Field(ge=1, le=600)


@app.post("/api/demo/advance")
def advance(body: ClockInput, s=Depends(auth)):
    require(s, "admin", "agent")
    with state(s["tenant"]) as d:
        d["offset"] += body.seconds
        tick(d)
        event(d, "demo.clock", f"演示时钟推进 {body.seconds} 秒", s["persona"])
        return {"now": now(d)}


@app.post("/api/demo/reset")
def reset(s=Depends(auth)):
    require(s, "admin")
    with state(s["tenant"]) as d:
        fresh = seed(s["tenant"])
        d.clear()
        d.update(fresh)
    return {"ok": True}


DIST = ROOT / "web" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/")
def index():
    if not (DIST / "index.html").exists():
        raise HTTPException(503, "请先在 web 目录运行 npm run build")
    return FileResponse(DIST / "index.html")
