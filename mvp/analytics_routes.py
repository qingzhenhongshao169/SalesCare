"""Explicit feedback and staff review; scoped to existing authentication."""
import math
from typing import Literal
from fastapi import APIRouter,Request,Depends,HTTPException
from pydantic import BaseModel,Field,ConfigDict
from sqlalchemy import select
from .security import principal,require
from .db import transaction,locked,rows
from .models import Conversation,Message,AnalyticsRecord,AnswerFeedback
from .service import get_conversation,audit
from .analytics import snapshot,project_many,business_question

router=APIRouter()
def auth(request:Request):return principal(request,write=request.method not in ('GET','HEAD','OPTIONS'))
class Input(BaseModel):model_config=ConfigDict(extra='forbid')
class ReviewInput(Input):
    sequence:int=Field(ge=0)
    data_kind:Literal['business','test','unknown']|None=None
    resolution:Literal['unknown','resolved','unresolved']|None=None
    evidence_message_id:str=Field(default='',max_length=40)
    confirmed_all:bool=False
    issue_key:str=Field(default='',max_length=100)
    issue_status:Literal['pending','checked','needs_knowledge']='checked'
class FeedbackInput(Input):value:Literal['solved','help','clear']

@router.get('/api/analytics')
def analytics(start:float=0,end:float=0,channel:str='all',data_kind:Literal['business','test','unknown','all']='business',p=Depends(auth)):
    require(p,'admin','agent')
    if not math.isfinite(start) or not math.isfinite(end) or start<0 or end and end<=start:raise HTTPException(422,'日期范围无效。')
    with transaction(p['tenant']) as s:return snapshot(s,p['tenant'],start,end,channel,data_kind)

@router.put('/api/analytics/conversations/{cid}')
def review(cid:str,body:ReviewInput,p=Depends(auth)):
    require(p,'admin','agent')
    with transaction(p['tenant']) as s:
        c=locked(s,Conversation,p['tenant'],cid)
        if not c:raise HTTPException(404,'会话不存在。')
        if body.sequence!=c.sequence:raise HTTPException(409,'会话已有新消息，请刷新后核查。')
        if body.data_kind is not None:require(p,'admin')
        if not body.issue_key and body.data_kind is None and body.resolution is None:raise HTTPException(422,'请选择核查操作。')
        view=project_many(s,p['tenant'],[c])[0]
        if view['deleted'] and (body.resolution is not None or body.issue_key):raise HTTPException(409,'内容已删除，无法继续核查原文。')
        if body.resolution and body.resolution!='unknown':
            m=s.get(Message,(p['tenant'],body.evidence_message_id))
            if not m or m.conversation_id!=cid:raise HTTPException(422,'请选择本会话中的核查依据。')
            if body.resolution=='resolved' and not body.confirmed_all:raise HTTPException(422,'请确认当前所有需求均已解决；部分解决应保留待确认。')
        if body.issue_key and not any(i['key']==body.issue_key for i in view['issues']):raise HTTPException(409,'复盘线索已变化，请刷新。')
        record=s.get(AnalyticsRecord,(p['tenant'],cid))
        if not record:record=AnalyticsRecord(tenant_id=p['tenant'],id=cid,reviews={});s.add(record)
        if body.data_kind is not None:record.data_kind=body.data_kind
        if body.resolution is not None:
            record.resolution=body.resolution;record.reviewed_sequence=c.sequence
            record.confirmed_all=body.confirmed_all;record.evidence_message_id=body.evidence_message_id
        if body.issue_key:record.reviews={**(record.reviews or {}),body.issue_key:{'status':body.issue_status,'sequence':c.sequence}}
        audit(s,p['tenant'],p['id'],'analytics.reviewed',cid,body.model_dump())
    return {'ok':True}

@router.get('/api/public/conversations/{cid}/feedback')
def feedback_list(cid:str,p=Depends(auth)):
    require(p,'visitor')
    with transaction(p['tenant']) as s:
        get_conversation(s,p,cid)
        return {f.id:f.value for f in s.scalars(rows(s,AnswerFeedback,p['tenant']).where(AnswerFeedback.conversation_id==cid))}

@router.put('/api/public/conversations/{cid}/feedback/{mid}')
def feedback(cid:str,mid:str,body:FeedbackInput,p=Depends(auth)):
    require(p,'visitor')
    with transaction(p['tenant']) as s:
        c=get_conversation(s,p,cid,for_update=True)
        if c.state=='CLOSED':raise HTTPException(409,'本次咨询已结束。')
        m=s.get(Message,(p['tenant'],mid))
        if not m or m.conversation_id!=cid or m.role!='ai':raise HTTPException(404,'回答不存在。')
        view=project_many(s,p['tenant'],[c])[0]
        if not view['ai_served']:raise HTTPException(422,'仅对业务回答提供反馈。')
        prior=s.scalar(rows(s,Message,p['tenant']).where(Message.conversation_id==cid,Message.role=='visitor',Message.sequence<m.sequence).order_by(Message.sequence.desc()))
        if not prior or not business_question(prior.text):raise HTTPException(422,'仅对业务回答提供反馈。')
        f=s.get(AnswerFeedback,(p['tenant'],mid))
        if body.value=='clear':
            if f:s.delete(f)
        elif f:f.value=body.value
        else:s.add(AnswerFeedback(tenant_id=p['tenant'],id=mid,conversation_id=cid,value=body.value))
        audit(s,p['tenant'],p['id'],'analytics.answer_feedback',cid,{'message_id':mid,'value':body.value,'scope':'answer_only'})
    return {'ok':True}
