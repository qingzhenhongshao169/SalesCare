import copy
import json

import pytest
from fastapi.testclient import TestClient

from server import app as backend
from server import domain


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB", tmp_path / "test.sqlite3")
    backend.sessions.clear()
    with TestClient(backend.app) as c:
        yield c


def identity(c, tenant="yunyu", persona="visitor"):
    r = c.post("/api/demo/session", json={"tenant": tenant, "persona": persona})
    assert r.status_code == 200
    return {"Authorization": "Bearer " + r.json()["token"]}


def start(c, h, channel="web"):
    r = c.post("/api/conversations", headers=h, json={"channel": channel})
    assert r.status_code == 200
    return r.json()["id"]


def chat(c, h, cid, text, key="m1"):
    return c.post(f"/api/conversations/{cid}/messages", headers=h, json={"text": text, "client_id": key})


def lead(c, h, cid):
    r = c.post(f"/api/conversations/{cid}/lead", headers=h, json={"name": "测试用户", "phone": "13800138000", "consent": True})
    assert r.status_code == 200, r.text
    return r.json()


def action(c, h, task, act, **kw):
    return c.post(f"/api/tasks/{task['id']}/{act}", headers=h, json={"version": task["version"], **kw})


def bootstrap(c, h):
    r = c.get("/api/bootstrap", headers=h)
    assert r.status_code == 200
    return r.json()


def test_unauthenticated_and_tenant_and_visitor_isolation(client):
    assert client.get("/api/bootstrap").status_code == 401
    v1 = identity(client)
    v2 = identity(client)
    other = identity(client, "hexu")
    cid = start(client, v1)
    assert chat(client, v2, cid, "查询").status_code == 404
    assert chat(client, other, cid, "查询").status_code == 404
    assert not bootstrap(client, other)["conversations"]
    assert all(c["id"] != cid for c in bootstrap(client, v2)["conversations"])
    # Tenant headers cannot override server-resolved context.
    forged = {**v1, "X-Tenant-ID": "hexu"}
    assert bootstrap(client, forged)["name"] == "云屿科技"


def test_known_unknown_profile_correction_and_retry_dedup(client):
    h = identity(client)
    cid = start(client, h)
    first = chat(client, h, cid, "35 人做项目协作，预算 300 元").json()
    assert first["messages"][-1]["citations"]
    assert first["profile"]["people"]["value"] == 35
    assert len(first["recommendations"]) == 1
    assert "多少人" not in first["messages"][-1]["text"]
    retry = chat(client, h, cid, "35 人做项目协作，预算 300 元").json()
    assert len(retry["messages"]) == len(first["messages"])
    correction = chat(client, h, cid, "预算改为 100 元", "m2").json()
    assert correction["profile"]["budget"]["value"] == 100
    assert not correction["recommendations"]
    unknown = chat(client, h, cid, "可以保证提升 50% 吗？我的电话 13800138000", "m3").json()
    assert unknown["messages"][-1]["unknown"]
    assert not unknown["messages"][-1]["citations"]
    editor = identity(client, persona="editor")
    output = bootstrap(client, editor)
    assert output["gaps"] and not output["conversations"] and not output["tasks"]
    assert "13800138000" not in json.dumps(output)


def test_consent_validation_and_idempotent_lead_transaction(client):
    h = identity(client)
    cid = start(client, h)
    assert client.post(f"/api/conversations/{cid}/lead", headers=h, json={"name": "用户", "consent": False}).status_code == 422
    t = lead(client, h, cid)
    assert t["deadline_at"] - t["accepted_at"] == 300
    t2 = lead(client, h, cid)
    assert t["id"] == t2["id"] and t["deadline_at"] == t2["deadline_at"]
    assert len(bootstrap(client, h)["tasks"]) == 1


def test_claim_not_contact_and_ai_freezes_and_reply_proves_reach(client):
    h = identity(client)
    cid = start(client, h)
    t = lead(client, h, cid)
    agent = identity(client, persona="lin")
    claimed = action(client, agent, t, "claim").json()
    assert claimed["attempted_at"] is None and claimed["sla"] == "running"
    replied = chat(client, h, cid, "请帮我看看预算", "after_claim").json()
    assert replied["messages"][-1]["role"] == "visitor"
    contacted = action(client, agent, claimed, "contact", text="根据您提出的协作需求，我来为您介绍方案。").json()
    assert contacted["sla"] == "met"
    assert contacted["evidence"][0]["type"] == "local_web_message_accepted"
    assert contacted["reached_at"] is None
    chat(client, h, cid, "收到，谢谢", "reply")
    assert bootstrap(client, h)["tasks"][0]["reached_at"] is not None


def test_ai_keeps_answering_while_waiting_without_completing_sla(client):
    h = identity(client)
    cid = start(client, h)
    t = lead(client, h, cid)
    response = chat(client, h, cid, "有哪些协作功能？", "pending").json()
    assert response["state"] == "HANDOFF_PENDING"
    assert response["messages"][-1]["role"] == "ai"
    assert response["messages"][-1]["waiting_for_human"]
    assert response["messages"][-1]["citations"]
    current_task = bootstrap(client, h)["tasks"][0]
    assert current_task["deadline_at"] == t["deadline_at"]
    assert current_task["attempted_at"] is None
    assert current_task["reached_at"] is None
    assert not current_task["evidence"]


def test_ai_resumes_during_transfer_until_next_agent_claims(client):
    h = identity(client)
    cid = start(client, h)
    t = lead(client, h, cid)
    lin = identity(client, persona="lin")
    claimed = action(client, lin, t, "claim").json()
    moved = action(client, lin, claimed, "transfer", target="zhou").json()
    response = chat(client, h, cid, "协作功能介绍", "transfer_pending").json()
    assert response["messages"][-1]["role"] == "ai"
    zhou = identity(client, persona="zhou")
    assert action(client, zhou, moved, "claim").status_code == 200
    response = chat(client, h, cid, "协作功能介绍", "claimed_again").json()
    assert response["messages"][-1]["role"] == "visitor"


def test_cas_and_transfer_keeps_deadline(client):
    h = identity(client)
    t = lead(client, h, start(client, h))
    lin, zhou = identity(client, persona="lin"), identity(client, persona="zhou")
    claimed = action(client, lin, t, "claim").json()
    assert action(client, zhou, t, "claim").status_code == 409
    assert action(client, zhou, claimed, "contact", text="您好").status_code == 403
    moved = action(client, lin, claimed, "transfer", target="zhou").json()
    assert moved["deadline_at"] == t["deadline_at"]
    assert moved["owner"] == "zhou" and not moved["claimed"]


def test_channel_failure_no_contact_no_deadline_reset(client):
    h = identity(client)
    t = lead(client, h, start(client, h, "wechat"))
    agent = identity(client, persona="lin")
    t = action(client, agent, t, "claim").json()
    admin = identity(client, persona="admin")
    client.patch("/api/channels/wechat", headers=admin, json={"window_open": False})
    assert action(client, agent, t, "contact", text="您好").status_code == 409
    stored = bootstrap(client, h)["tasks"][0]
    assert stored["deadline_at"] == t["deadline_at"]
    assert not stored["evidence"] and stored["attempted_at"] is None


def test_withdraw_blocks_contact_and_closed_session(client):
    h = identity(client)
    cid = start(client, h)
    t = lead(client, h, cid)
    agent = identity(client, persona="lin")
    t = action(client, agent, t, "claim").json()
    assert client.post(f"/api/conversations/{cid}/withdraw", headers=h).status_code == 200
    stored = bootstrap(client, h)["tasks"][0]
    assert stored["phone"] == "" and not stored["consent"]
    assert action(client, agent, stored, "contact", text="您好").status_code == 409
    assert chat(client, h, cid, "又发消息").status_code == 409
    assert client.post(f"/api/conversations/{cid}/handoff", headers=h).status_code == 409


def test_sla_boundary_300_inclusive_and_late_breach_retained(monkeypatch):
    monkeypatch.setattr(domain.time, "time", lambda: 1000000.)
    d = domain.seed("yunyu")
    c = domain.make_conversation(d, "v", "web")
    t = domain.make_task(d, c, "边界用户")
    d["offset"] = 300
    domain.tick(d)
    assert t["sla"] == "running"
    t["attempted_at"] = domain.now(d)
    domain.tick(d)
    assert t["sla"] == "met"
    t["attempted_at"] += .001
    domain.tick(d)
    assert t["sla"] == "breached"
    t.update({"status": "cancelled", "cancelled_at": domain.now(d) + 1})
    domain.tick(d)
    assert t["sla"] == "breached"


def test_escalations_at_most_two_transfers_and_clock_persists(client):
    h = identity(client)
    cid = start(client, h)
    t = lead(client, h, cid)
    admin = identity(client, persona="admin")
    client.post("/api/demo/advance", headers=admin, json={"seconds": 305})
    stored = bootstrap(client, h)["tasks"][0]
    assert stored["reassignments"] == 2
    assert stored["sla"] == "breached"
    assert stored["deadline_at"] == t["deadline_at"]
    with backend.state("yunyu") as d:
        assert d["offset"] == 305
    assert bootstrap(client, h)["tasks"][0]["reassignments"] == 2


def test_no_staff_no_promise_and_handoff_without_lead(client):
    h = identity(client)
    cid = start(client, h)
    admin = identity(client, persona="admin")
    for member in ("lin", "zhou"):
        client.patch(f"/api/members/{member}", headers=admin, json={"available": False})
    r = client.post(f"/api/conversations/{cid}/lead", headers=h, json={"name": "用户", "consent": True})
    assert r.status_code == 409
    handoff = client.post(f"/api/conversations/{cid}/handoff", headers=h).json()
    assert handoff["deadline_at"] is None
    assert not handoff["consent"]


def test_knowledge_draft_publish_revoke_and_high_risk_review(client):
    editor, publisher = identity(client, persona="editor"), identity(client, persona="publisher")
    admin = identity(client, persona="admin")
    h = identity(client)
    cid = start(client, h)
    k = client.post("/api/knowledge", headers=admin, json={"title": "海外支持价格", "content": "海外支持服务 900 元。", "keywords": "海外支持", "risk": "low"}).json()
    assert k["risk"] == "high"
    assert chat(client, h, cid, "海外支持", "a").json()["messages"][-1]["unknown"]
    assert client.post(f"/api/knowledge/{k['id']}/publish", headers=editor).status_code == 403
    assert client.post(f"/api/knowledge/{k['id']}/publish", headers=admin).status_code == 409
    assert client.post(f"/api/knowledge/{k['id']}/publish", headers=publisher).status_code == 200
    assert chat(client, h, cid, "海外支持", "b").json()["messages"][-1]["citations"][0]["id"] == k["id"]
    client.post(f"/api/knowledge/{k['id']}/revoke", headers=publisher)
    assert chat(client, h, cid, "海外支持", "c").json()["messages"][-1]["unknown"]


@pytest.mark.parametrize("change", [{"status":"draft"}, {"visibility":"internal"}, {"expires_at":1}, {"channels":["wechat"]}])
def test_nonpublic_expired_wrong_channel_knowledge_excluded(client, change):
    h = identity(client)
    cid = start(client, h)
    with backend.state("yunyu") as d:
        d["knowledge"][0].update(change)
    result = chat(client, h, cid, "产品价格 35 人预算 300 元", "a").json()
    assert result["messages"][-1]["unknown"]
    assert not result["recommendations"]


def test_candidate_update_is_atomic_and_recommendations_stay_valid(client):
    admin = identity(client, persona="admin")
    publisher = identity(client, persona="publisher")
    assert client.post("/api/knowledge/kb0/sync", headers=admin).status_code == 200
    d = bootstrap(client, admin)
    draft = next(k for k in d["knowledge"] if k.get("replaces") == "kb0")
    assert next(k for k in d["knowledge"] if k["id"] == "kb0")["status"] == "published"
    assert client.post(f"/api/knowledge/{draft['id']}/publish", headers=publisher).status_code == 200
    d = bootstrap(client, admin)
    assert next(k for k in d["knowledge"] if k["id"] == "kb0")["status"] == "superseded"
    h = identity(client)
    result = chat(client, h, start(client, h), "35 人项目协作，预算 300 元").json()
    assert result["recommendations"]


def test_staff_pii_scope_role_enforcement_and_tenant_reset(client):
    h = identity(client)
    t = lead(client, h, start(client, h))
    admin = identity(client, persona="admin")
    agent = identity(client, persona="lin")
    assert all(x["phone"] == "已保护" for x in bootstrap(client, admin)["tasks"])
    assert action(client, admin, t, "claim").status_code == 403
    action(client, agent, t, "claim")
    assert next(x for x in bootstrap(client, agent)["tasks"] if x["id"] == t["id"])["phone"] == "13800138000"
    other = identity(client, "hexu", "admin")
    ids = [x["id"] for x in bootstrap(client, other)["conversations"]]
    client.post("/api/demo/reset", headers=admin)
    assert ids == [x["id"] for x in bootstrap(client, other)["conversations"]]


def test_restart_keeps_business_state(client):
    h = identity(client)
    t = lead(client, h, start(client, h))
    backend.initialize()
    backend.sessions.clear()
    stored = bootstrap(client, h)["tasks"][0]
    assert stored["id"] == t["id"] and stored["deadline_at"] == t["deadline_at"]


def test_new_consent_after_online_contact_starts_new_commitment(client):
    h = identity(client)
    cid = start(client, h)
    online = client.post(f"/api/conversations/{cid}/handoff", headers=h).json()
    agent = identity(client, persona="lin")
    claimed = action(client, agent, online, "claim").json()
    contacted = action(client, agent, claimed, "contact", text="您好，可以在线为您介绍协作方案。").json()
    assert contacted["attempted_at"] is not None and contacted["deadline_at"] is None
    committed = lead(client, h, cid)
    assert committed["id"] != online["id"]
    assert committed["deadline_at"] - committed["accepted_at"] == 300
    assert committed["attempted_at"] is None
    stored = bootstrap(client, h)
    assert stored["conversations"][0]["lead"]["consent"]
    assert len(stored["tasks"]) == 2
    assert next(t for t in stored["tasks"] if t["id"] == online["id"])["evidence"]
