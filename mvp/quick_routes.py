"""Publisher-approved customer replies bound to a currently published fact source."""
import re
import time
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from .db import transaction,locked
from .models import Knowledge,KBChunk
from .security import principal,require,serialize
from .service import audit,risk
from .quick_replies import question_key
from .knowledge_provenance import digest
from server.answer_policy import numbers_supported,date_ranges_supported
from server.model_gateway import unauthorized_action_or_promise

router=APIRouter()


class QuickInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    version:int=Field(ge=1)
    enabled:bool=True
    questions:list[str]=Field(default=[],max_length=20)
    answer:str=Field(default='',max_length=1000)
    confirmed:bool=False


@router.put('/api/quick-replies/{kid}')
def configure(kid:str,body:QuickInput,request:Request):
    p=principal(request,write=True);require(p,'admin','publisher')
    with transaction(p['tenant']) as s:
        k=locked(s,Knowledge,p['tenant'],kid)
        if not k:raise HTTPException(404,'知识不存在。')
        if k.version!=body.version:raise HTTPException(409,'来源版本已变化，请刷新后核对。')
        if not body.enabled:
            k.quick_reply={};audit(s,p['tenant'],p['id'],'quick_reply.disabled',kid);return serialize(k)
        if k.status!='published' or k.visibility!='public' or (k.expires_at and k.expires_at<=time.time()):raise HTTPException(409,'仅可配置当前有效的已发布对客知识。')
        if s.get(KBChunk,(p['tenant'],kid)):raise HTTPException(409,'请将标准FAQ整理为独立知识条目审核，文档分块保留原资料管理流程。')
        if not body.confirmed:raise HTTPException(422,'请确认问题和答案已核对，可直接对客户展示。')
        questions=list(dict.fromkeys(q.strip() for q in body.questions if q.strip()))
        if not questions or any(len(q)>200 or not question_key(q) for q in questions):raise HTTPException(422,'请填写1至20个明确问题，每题不超过200字。')
        answer=body.answer.strip()
        if not answer or re.search(r'先确认客户|再引用本|提示词|知识库.{0,8}(?:缺少|没有)|不同规格和不同商品不能合并',answer):raise HTTPException(422,'请填写可直接对客户说的答案，不使用内部操作指引。')
        if not numbers_supported(answer,k.content,[]) or not date_ranges_supported(answer,[{'content':k.content}]):raise HTTPException(422,'答案包含来源未支持的数字或日期。')
        if unauthorized_action_or_promise(answer):raise HTTPException(422,'答案包含未经授权的操作或效果承诺。')
        if risk(answer)=='high' and k.author_id==p['id']:raise HTTPException(403,'高风险快捷答案需另一位发布者核对。')
        k.quick_reply={'enabled':True,'questions':questions,'answer':answer,'source_digest':digest(k.content),
                       'source_version':k.version,'reviewer_id':p['id'],'reviewed_at':time.time()}
        audit(s,p['tenant'],p['id'],'quick_reply.approved',kid,{'version':k.version,'questions':len(questions)})
        return serialize(k)
