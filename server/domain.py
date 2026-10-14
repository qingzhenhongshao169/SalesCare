"""Portable demo domain. All tenant documents are mutated in SQLite transactions."""
import copy
import re
import time
import uuid


def uid(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def event(d, action, detail, actor="system"):
    d["audit"].insert(0, {"id": uid("evt"), "at": now(d), "action": action, "detail": detail, "actor": actor})
    d["audit"] = d["audit"][:400]


def now(d):
    return time.time() + d.get("offset", 0)


def seed(tenant):
    if tenant == "biaofun":
        from .company_knowledge import apply_company
        d = apply_company(seed("yunyu"))
        event(d, "knowledge.imported", "标梵官网知识已整理导入；价格、效果与冲突政策保留待审核。")
        return d
    consulting = tenant == "hexu"
    stamp = time.time()
    name = "禾序咨询" if consulting else "云屿科技"
    skill = "企业咨询" if consulting else "协作方案"
    d = {"id": tenant, "name": name, "industry": "企业咨询服务" if consulting else "协作软件与解决方案", "offset": 0,
         "tagline": "让每一次咨询，都有好的回应。", "conversations": [], "tasks": [], "gaps": [], "audit": [],
         "members": [{"id": "lin", "name": "林知夏", "title": "专业顾问", "skill": skill, "available": True},
                     {"id": "zhou", "name": "周予安", "title": "专业顾问", "skill": skill, "available": True}],
         "channels": [{"id": "web", "name": "网站客服", "subtitle": "官网嵌入 · H5 访客入口", "connected": True, "window_open": True, "mode": "local", "authority": "saas"},
                      {"id": "wechat", "name": "微信客服", "subtitle": "独立客服入口 · 模拟适配器", "connected": True, "window_open": True, "mode": "simulation", "authority": "saas"},
                      {"id": "wecom", "name": "企微客户联系", "subtitle": "接待能力待授权核验 · 场景模拟", "connected": True, "window_open": True, "mode": "simulation", "authority": "native"},
                      {"id": "dingtalk", "name": "钉钉机器人", "subtitle": "机器人接待 · 场景模拟", "connected": True, "window_open": True, "mode": "simulation", "authority": "saas"}]}
    offers = ([
        ("经营诊断", "service", 3000, "从访谈与业务数据开始，梳理当前经营问题。", "单次诊断服务；不承诺经营收益。", ["诊断", "分析", "咨询"], 200),
        ("运营增长陪跑", "service", 15000, "为已有团队提供 4 周运营梳理与执行支持。", "需先完成需求评估；不承诺增长比例。", ["增长", "运营", "陪跑"], 2000),
    ] if consulting else [
        ("协作标准版", "product", 199, "适合 50 人以内团队，统一任务、文档与项目协作。", "199 元／月，最多 50 人；不含专属实施服务。", ["协作", "任务", "项目", "文档"], 50),
        ("企业专业版", "product", 699, "支持 300 人团队、多项目管理与专属实施顾问。", "699 元／月，最多 300 人；特殊部署需单独确认。", ["协作", "项目", "实施", "权限"], 300),
    ])
    d["offers"] = [{"id": f"offer{i}", "name": x[0], "type": x[1], "price": x[2], "description": x[3], "limits": x[4], "tags": x[5], "max_people": x[6], "active": True} for i, x in enumerate(offers)]
    facts = ([
        ("服务目录与费用", "经营诊断为 3,000 元／次；运营增长陪跑为 15,000 元／4 周。具体服务范围需要顾问确认，不承诺经营收益。", ["价格", "费用", "多少钱", "预算", "诊断", "陪跑"], "high"),
        ("需求访谈与交付流程", "经营诊断包括需求访谈、问题梳理和一份建议报告；陪跑先评估需求，再确认范围。您当前最希望解决什么经营问题？", ["流程", "服务", "运营", "增长", "咨询"], "low"),
    ] if consulting else [
        ("产品版本与公开价格", "协作标准版 199 元／月，支持最多 50 人；企业专业版 699 元／月，支持最多 300 人并包含专属实施顾问。特殊部署与额外服务需要单独确认。", ["价格", "费用", "多少钱", "预算", "版本", "人数", "团队"], "high"),
        ("团队协作与项目管理", "两个版本均提供任务管理、文档协作与项目看板。企业专业版另有多项目管理与专属实施顾问。您大约有多少人使用？", ["功能", "协作", "项目", "文档", "任务", "实施"], "low"),
    ])
    facts += [("专业人工服务说明", "本演示企业设置为全天值班。提交并同意通过当前会话联系后，专业顾问将在 5 分钟内发起联系；是否触达取决于您的回应。", ["人工", "联系", "顾问", "时间", "客服"], "low")]
    d["knowledge"] = [{"id": f"kb{i}", "family": f"kb{i}", "title": f[0], "content": f[1], "keywords": f[2], "risk": f[3], "status": "published", "version": 1,
                        "visibility": "public", "channels": ["web", "wechat", "wecom", "dingtalk"], "author": "editor", "reviewer": "publisher", "source": "企业已审核资料（演示）", "updated_at": stamp, "expires_at": None} for i, f in enumerate(facts)]
    d["knowledge"].append({**copy.deepcopy(d["knowledge"][1]), "id": "kb_draft", "title": "新客户常见问题补充", "content": "欢迎预约一次需求沟通，由顾问协助确认适用方案。", "keywords": ["预约"], "status": "draft", "version": 1, "reviewer": None})
    for i, (visitor, channel, ago, state) in enumerate([("陈女士", "web", 210, "pending"), ("许先生", "wechat", 80, "pending"), ("赵女士", "dingtalk", 360, "pending"), ("王先生", "web", 480, "met")]):
        text = "我们想先做一次经营诊断，预算 5000 元。" if consulting else "我们有 35 人，想用项目协作，预算 300 元。"
        c = make_conversation(d, "seed", channel, visitor)
        c["messages"].append({"id": uid("msg"), "role": "visitor", "text": text, "at": stamp - ago - 30})
        profile(d, c, text)
        c["messages"].append({"id": uid("msg"), "role": "ai", "text": facts[0][1], "at": stamp - ago - 29, "citations": [{"id": "kb0", "title": facts[0][0], "version": 1}]})
        task = make_task(d, c, visitor, "1380000000" + str(i), accepted=stamp - ago)
        if state == "met":
            task.update({"owner": "lin", "claimed": True, "attempted_at": stamp - ago + 95, "reached_at": stamp - ago + 110, "status": "contacted", "evidence": [{"at": stamp - ago + 95, "type": "demo_channel_accepted", "actor": "lin", "id": uid("receipt")} ]})
            c["state"] = "HUMAN_ACTIVE"
            c["messages"].append({"id": uid("msg"), "role": "human", "text": "您好，我是专业顾问林知夏。我已看过您的需求，可以为您说明适用方案。", "at": stamp - ago + 95})
    event(d, "demo.seed", "已载入两个行业模板之一；所有联系人、资料和回执均为演示数据。")
    tick(d)
    return d


def make_conversation(d, visitor_id, channel, name="新访客"):
    c = {"id": uid("conv"), "visitor_id": visitor_id, "name": name, "channel": channel, "state": "AI_ACTIVE", "created_at": now(d), "profile": {}, "messages": [], "recommendations": [], "lead": None}
    d["conversations"].insert(0, c)
    return c


def make_task(d, c, name, phone="", accepted=None, consent=True):
    accepted = now(d) if accepted is None else accepted
    old = next((t for t in d["tasks"] if t["conversation_id"] == c["id"] and t["status"] != "cancelled"
                and (not consent or t["deadline_at"] is not None or t["status"] == "pending")), None)
    if old:
        return old
    available = [m for m in d["members"] if m["available"]]
    t = {"id": uid("task"), "conversation_id": c["id"], "name": name, "channel": c["channel"], "skill": d["members"][0]["skill"], "accepted_at": accepted,
         "deadline_at": accepted + 300 if consent else None, "status": "pending", "sla": "running" if consent else "none", "owner": available[0]["id"] if available else None,
         "claimed": False, "version": 1, "attempted_at": None, "reached_at": None, "evidence": [], "escalations": [], "consent": consent, "phone": phone,
         "method": "current_channel", "reassignments": 0}
    d["tasks"].insert(0, t)
    c.update({"name": name, "state": "HANDOFF_PENDING", "lead": {"name": name, "phone": phone, "consent": consent, "accepted_at": accepted} if consent else None})
    event(d, "lead.accepted" if consent else "handoff.requested", f"{name} · {'已启动 300 秒联系承诺' if consent else '在线人工接待，无回访授权'}")
    return t


def tick(d):
    stamp = now(d)
    for t in d["tasks"]:
        if not t["deadline_at"]:
            continue
        if t["status"] == "cancelled":
            if t["attempted_at"]:
                t["sla"] = "met" if t["attempted_at"] <= t["deadline_at"] else "breached"
            elif t.get("cancelled_at", stamp) <= t["deadline_at"]:
                t["sla"] = "cancelled"
            else:
                t["sla"] = "breached"
            continue
        if t["attempted_at"]:
            t["sla"] = "met" if t["attempted_at"] <= t["deadline_at"] else "breached"
            continue
        t["sla"] = "breached" if stamp > t["deadline_at"] else "running"
        elapsed = stamp - t["accepted_at"]
        for threshold, label in [(30, "未接单提醒"), (60, "首次转派检查"), (120, "二次转派检查"), (180, "升级主管"), (240, "紧急预警"), (301, "已超时，持续跟进")]:
            if elapsed >= threshold and threshold not in t["escalations"]:
                t["escalations"].append(threshold)
                if threshold in (60, 120) and not t["claimed"]:
                    eligible = [m for m in d["members"] if m["available"] and m["skill"] == t["skill"] and m["id"] != t["owner"]]
                    if eligible:
                        t["owner"] = eligible[0]["id"]
                        t["reassignments"] += 1
                        t["version"] += 1
                event(d, "sla.escalation", f"{t['name']} · {label}")


def profile(d, c, text):
    if d["id"] == "biaofun":
        return company_profile(d, c, text)
    fields = {}
    people = re.search(r"(\d+)\s*人", text)
    budget = re.search(r"预算(?:是|为|改为|改成|只有|约|大概)?\s*(\d+(?:\.\d+)?)\s*(万|千)?", text)
    if people:
        fields["people"] = {"label": "团队规模", "value": int(people[1]), "display": f"{people[1]} 人"}
    if budget:
        amount = float(budget[1]) * {"万": 10000, "千": 1000, None: 1}[budget[2]]
        fields["budget"] = {"label": "预算上限", "value": amount, "display": f"¥{amount:,.0f}"}
    # Only literal statements become facts; no inferred age, income or identity.
    topics = [x for x in ["项目协作", "项目", "文档", "任务", "经营诊断", "诊断", "增长", "运营", "陪跑"] if x in text]
    if topics:
        fields["need"] = {"label": "当前需求", "value": topics[0], "display": topics[0]}
    if "下周" in text or "本月" in text:
        fields["timeline"] = {"label": "计划时间", "value": "下周" if "下周" in text else "本月", "display": "下周" if "下周" in text else "本月"}
    for key, value in fields.items():
        c["profile"][key] = {**value, "evidence": text, "source": "用户原话", "confirmed": True}
    budget_value = c["profile"].get("budget", {}).get("value")
    people_value = c["profile"].get("people", {}).get("value", 0)
    need = c["profile"].get("need", {}).get("value", "")
    c["recommendations"] = []
    price_active = any(k["status"] == "published" and (k["id"] == "kb0" or k.get("family") == "kb0") and k["visibility"] == "public" and c["channel"] in k["channels"] and (not k["expires_at"] or k["expires_at"] > now(d)) for k in d["knowledge"])
    if not price_active or not need:
        return
    for offer in d["offers"]:
        if offer["active"] and any(tag in need for tag in offer["tags"]) and people_value <= offer["max_people"] and (budget_value is None or offer["price"] <= budget_value):
            c["recommendations"].append({"offer_id": offer["id"], "reason": f"与「{need}」相关" + ("，符合已说明的预算" if budget_value is not None else "；预算尚待确认")})
    c["recommendations"] = c["recommendations"][:3]


def reply(d, c, text):
    profile(d, c, text)
    if d["id"] == "biaofun":
        from .retrieval import retrieve, citation
        injection = any(x in text.lower() for x in ["忽略规则", "忽略之前", "提示词", "api密钥", "api key", "其他企业", "其他租户", "system prompt"])
        records = [] if injection else retrieve(d, c["channel"], text, now(d))
        if records:
            k = records[0]
            used = records[:2] if re.search(r"和|以及|同时|还有|、", text) else [k]
            return {"id":uid("msg"), "role":"ai", "at":now(d), "text":"\n\n".join(x["content"] for x in used),
                    "citations":[citation(x) for x in used], "engine":"knowledge-retrieval"}
    valid = [k for k in d["knowledge"] if k["status"] == "published" and k["visibility"] == "public" and c["channel"] in k["channels"] and (not k["expires_at"] or k["expires_at"] > now(d))]
    unsupported = any(word in text for word in ["保证", "赔偿", "折扣", "免费", "私有", "退款", "稳赚", "忽略", "提示词", "其他企业", "降价", "返还"])
    matches = sorted([(sum(word in text for word in k["keywords"]), k) for k in valid], key=lambda item: item[0], reverse=True)
    if d["id"] != "biaofun" and matches and matches[0][0] > 0 and not unsupported:
        k = matches[0][1]
        answer_text = k["content"]
        if "people" in c["profile"]:
            answer_text = answer_text.replace("您大约有多少人使用？", "")
        if "need" in c["profile"]:
            answer_text = answer_text.replace("您当前最希望解决什么经营问题？", "")
        answer = {"text": answer_text, "citations": [{"id": k["id"], "title": k["title"], "version": k["version"]}]}
    else:
        answer = {"text": "这个细节需要由专业同事进一步确认，目前已发布资料不足以给出准确结论。您可以补充使用场景，也可以直接转人工，我会把当前需求一并交给顾问。", "citations": [], "unknown": True}
        # Gap review exposes a category only; raw questions stay in the authorized conversation.
        category = "价格与特殊承诺待核实" if unsupported else "知识范围外的问题"
        gap = next((g for g in d["gaps"] if g["title"] == category and g["status"] == "open"), None)
        if gap:
            gap["count"] += 1
        else:
            d["gaps"].insert(0, {"id": uid("gap"), "title": category, "excerpt": "请由获授权的接待人员核对原问题后，补充不含个人信息的知识候选。", "count": 1, "status": "open", "at": now(d)})
        event(d, "knowledge.gap", category)
    return {"id": uid("msg"), "role": "ai", "at": now(d), **answer}


def company_profile(d, c, text):
    fields = {}
    people = re.search(r"(\d+)\s*人", text)
    budget = re.search(r"预算(?:是|为|改为|改成|只有|约|大概)?\s*(\d+(?:\.\d+)?)\s*(万|千)?", text)
    if people: fields["people"] = {"label":"团队规模", "value":int(people[1]), "display":f"{people[1]} 人"}
    if budget:
        value=float(budget[1])*{None:1,"千":1000,"万":10000}[budget[2]]
        fields["budget"]={"label":"预算范围", "value":value, "display":f"¥{value:,.0f}"}
    # Literal service interests only; no inferred personal demographics or affordability.
    words=[tag for o in d["offers"] for tag in o["tags"] if tag.lower() in text.lower()]
    if words:
        need="、".join(dict.fromkeys(words))
        fields["need"]={"label":"咨询方向", "value":need, "display":need}
    for key,value in fields.items(): c["profile"][key]={**value,"evidence":text,"source":"用户原话","confirmed":True}
    need=c["profile"].get("need",{}).get("value","")
    active_urls={k.get("source_url") for k in d["knowledge"] if k["status"]=="published" and k["visibility"]=="public" and c["channel"] in k["channels"] and (not k.get("expires_at") or k["expires_at"]>now(d))}
    c["recommendations"]=[{"offer_id":o["id"],"reason":"与已说明的咨询方向相关；范围和报价需评估"}
                          for o in d["offers"] if o["active"] and o.get("source_url") in active_urls and need and any(tag in need for tag in o["tags"])][:3]


def metrics(d):
    ts = now(d)
    due = [t for t in d["tasks"] if t["deadline_at"] and t["deadline_at"] <= ts]
    cancelled = [t for t in due if t["sla"] == "cancelled"]
    base = [t for t in due if t["sla"] != "cancelled"]
    met = sum(t["sla"] == "met" for t in base)
    return {"conversations": len(d["conversations"]), "leads": sum(c["lead"] is not None for c in d["conversations"]),
            "pending": sum(t["status"] == "pending" for t in d["tasks"]), "breached": sum(t["sla"] == "breached" for t in d["tasks"]),
            "due": len(base), "met": met, "excluded": len(cancelled), "rate": round(met / len(base) * 100) if base else None,
            "attempted": sum(t["attempted_at"] is not None for t in d["tasks"]), "reached": sum(t["reached_at"] is not None for t in d["tasks"]),
            "published": sum(k["status"] == "published" for k in d["knowledge"]), "gaps": len([g for g in d["gaps"] if g["status"] == "open"])}
