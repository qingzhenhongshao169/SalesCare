"""Reviewed historical cases use the existing knowledge lifecycle, not product prices."""
import json
import time
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from .config import ROOT
from .db import transaction, locked, serialization_lock
from .models import Knowledge, CompanySettings, Tenant, KBChunk, uid
from .security import principal, require, serialize
from .service import audit, risk
from .case_recommendations import SERVICES, INDUSTRIES, GOALS, safe_url, policy
from .knowledge_provenance import digest
from server.model_gateway import unauthorized_action_or_promise

router = APIRouter()


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class CaseInput(Input):
    version: int = Field(ge=1)
    enabled: bool = True
    confirmed: bool = False
    summary: str = Field(default='', max_length=600)
    service_types: list[str] = Field(default=[], max_length=5)
    industries: list[str] = Field(default=[], max_length=8)
    goals: list[str] = Field(default=[], max_length=7)
    priority: int = Field(default=50, ge=0, le=100)

    @model_validator(mode='after')
    def supported_tags(self):
        for values, allowed in ((self.service_types, SERVICES), (self.industries, INDUSTRIES), (self.goals, GOALS)):
            if set(values) - set(allowed):
                raise ValueError('Unknown case classification')
        return self


class Weights(Input):
    service: int = Field(default=35, ge=1, le=100)
    goals: int = Field(default=30, ge=0, le=100)
    industry: int = Field(default=25, ge=0, le=100)
    priority: int = Field(default=10, ge=0, le=100)

    @model_validator(mode='after')
    def total(self):
        if sum(self.model_dump().values()) != 100:
            raise ValueError('Weights must sum to 100')
        return self


class PolicyInput(Input):
    enabled: bool = True
    min_business_turns: int = Field(default=3, ge=2, le=6)
    max_items: int = Field(default=1, ge=1, le=2)
    weights: Weights = Field(default_factory=Weights)


@router.get('/api/case-recommendations/settings')
def get_settings(request: Request):
    p = principal(request); require(p, 'admin', 'publisher')
    with transaction(p['tenant']) as s:
        cfg = s.get(CompanySettings, (p['tenant'], 'primary'))
        return {'policy': policy(cfg.case_recommendation_policy),
                'service_types': {k: v[0] for k, v in SERVICES.items()},
                'industries': {k: v[0] for k, v in INDUSTRIES.items()},
                'goals': {k: v[0] for k, v in GOALS.items()}}


@router.put('/api/case-recommendations/settings')
def set_settings(body: PolicyInput, request: Request):
    p = principal(request, write=True); require(p, 'admin')
    with transaction(p['tenant']) as s:
        cfg = locked(s, CompanySettings, p['tenant'], 'primary')
        cfg.case_recommendation_policy = body.model_dump()
        audit(s, p['tenant'], p['id'], 'case.policy_changed', 'primary', body.model_dump())
    return body.model_dump()


@router.put('/api/case-recommendations/{kid}')
def configure(kid: str, body: CaseInput, request: Request):
    p = principal(request, write=True); require(p, 'admin', 'publisher')
    with transaction(p['tenant']) as s:
        k = locked(s, Knowledge, p['tenant'], kid)
        if not k:
            raise HTTPException(404, '知识不存在。')
        if k.version != body.version:
            raise HTTPException(409, '来源版本已变化，请刷新后核对。')
        if not body.enabled:
            k.case_recommendation = {**(k.case_recommendation or {}), 'enabled': False}
            audit(s, p['tenant'], p['id'], 'case.disabled', kid)
            return serialize(k)
        if k.status != 'published' or k.visibility != 'public' or k.expires_at and k.expires_at <= time.time():
            raise HTTPException(409, '请先审核发布有效的对客案例知识。')
        if s.get(KBChunk, (p['tenant'], kid)):
            raise HTTPException(409, '请将案例整理为独立知识审核，保留文档分块原有管理流程。')
        cfg = s.get(CompanySettings, (p['tenant'], 'primary'))
        if not safe_url(k.source_url, cfg.source_hosts):
            raise HTTPException(422, '案例链接须为企业白名单内的HTTPS原始来源。')
        summary = body.summary.strip()
        if not body.confirmed or not body.service_types or not summary or summary not in k.content:
            raise HTTPException(422, '请确认分类，并从已审核正文中选取连续原文作为案例简介。')
        if unauthorized_action_or_promise(summary):
            raise HTTPException(422, '案例简介不能包含未经授权的操作或效果承诺。')
        if risk(summary) == 'high' and k.author_id == p['id']:
            raise HTTPException(403, '高风险内容需另一位发布者核对。')
        k.case_recommendation = {**body.model_dump(exclude={'version', 'confirmed'}), 'summary': summary,
            'source_digest': digest(k.content), 'source_version': k.version, 'source_url': k.source_url,
            'reviewer_id': p['id'], 'reviewed_at': time.time()}
        audit(s, p['tenant'], p['id'], 'case.approved', kid, {'version': k.version})
        return serialize(k)


class ImportInput(Input):
    apply: bool = False


@router.post('/api/case-catalogue/import')
def import_catalogue(body: ImportInput, request: Request):
    p = principal(request, write=True); require(p, 'admin', 'editor')
    with transaction(p['tenant']) as s:
        if s.get(Tenant, p['tenant']).slug != 'biaofun':
            raise HTTPException(403, '标梵官网资料包仅供标梵企业导入，不复制到其他企业。')
        if body.apply:
            serialization_lock(s, p['tenant'], 'case-catalogue:20261019')
        package = json.loads((ROOT/'knowledge/biaofun-case-studies-20261019.json').read_text('utf-8'))
        existing = set(s.scalars(select(Knowledge.source_url).where(Knowledge.tenant_id == p['tenant'])))
        imported, skipped = [], []
        for entry in package['entries']:
            if entry['source_url'] in existing:
                skipped.append(entry['id']); continue
            if not entry.get('source_verified'):
                skipped.append(entry['id']); continue
            if body.apply:
                s.add(Knowledge(tenant_id=p['tenant'], id=uid(), family=uid(), title=entry['title'],
                    content=entry['content'], source_url=entry['source_url'], category='项目案例',
                    keywords=entry['keywords'], author_id=p['id'], status='draft', visibility='public', risk=risk(entry['content']),
                    case_recommendation={**entry['recommendation'], 'enabled': False}))
            imported.append(entry['id'])
        if body.apply:
            audit(s, p['tenant'], p['id'], 'case.catalogue_imported', '20261019', {'draft_count': len(imported)})
        return {'applied': body.apply, 'drafts': imported, 'skipped': skipped,
                'message': '只导入待审资料；发布后还需核对案例分类并启用推荐。'}
