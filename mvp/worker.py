"""Independent, lease-based AI, knowledge synchronization and SLA worker."""
import concurrent.futures
import logging
import re
import signal
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select, func, exists
from sqlalchemy.orm import aliased
from fastapi import HTTPException
from server import model_gateway
from server.retrieval import citation
from server.dialogue_policy import dialogue_context, POLICY_VERSION
from server.turn_plan import build_turn_plan, coverage_issue, task_fallback
from .db import transaction, rows, locked, database_time, serialization_lock
from .models import *
from .service import company, add_message, audit, redacted_text, risk, candidates
from .security import serialize
from .sources import fetch_public
from .answering import social_intent,social_fallback,conversational_answer,resolve_question,operational_reply,contextual_fallback,grounded_fallback,support_fallback
from .reception import notification_current,missing_cycle_contact_expression
from .knowledge_retrieval import search as search_knowledge, revalidate
from .knowledge_provenance import evidence_summary
from .agent_flow import ReceptionFlow, FlowExpired
from .ai_resilience import model_health,CUSTOMER_WAIT_SECONDS
from .notifications import send_notification
from .realtime import PgListener,JOB_CHANNEL
from .quick_replies import lookup as quick_lookup,still_current as quick_current

log=logging.getLogger('salescare.worker')
STOP=threading.Event()
WAKE=threading.Event()
WORKER_ID=uid()

def tenant_ids():
    with transaction() as session:
        return list(session.scalars(select(Tenant.id).where(Tenant.active==True)))

def tick_sla(tid):
    with transaction(tid) as session:
        now=database_time(session)
        tasks=session.scalars(rows(session,Task,tid).where(Task.deadline_at.is_not(None),Task.status.notin_(['cancelled','closed']),missing_cycle_contact_expression()).with_for_update(skip_locked=True)).all()
        for t in tasks:
            if t.attempted_at is not None:
                audit(session,tid,'sla-worker','task.pre_cycle_contact_ignored',t.id,
                      {'attempted_at':t.attempted_at,'reached_at':t.reached_at,'start_at':t.start_at})
                t.attempted_at=None;t.reached_at=None
            if now<t.start_at: continue
            t.sla='breached' if now>t.deadline_at else 'running'
            elapsed=now-t.start_at
            steps=[(30,'未接单提醒'),(60,'转派检查'),(120,'再次转派检查'),(180,'升级主管'),(240,'紧急跟进'),(301,'联系已超时，请继续跟进')]
            # After an outage emit the current highest alert, not every old alert.
            due=[x for x in steps if elapsed>=x[0]]
            if not due: continue
            threshold,label=due[-1]
            if t.status=='claimed' and threshold in (30,60,120):label='已接待但尚未专业回复，请继续跟进'
            key=f'sla:{t.id}:{threshold}'
            if session.scalar(rows(session,Outbox,tid).where(Outbox.key==key)): continue
            if threshold in (60,120) and t.status=='pending':
                choices=[m for m in candidates(session,tid,t.skill,now) if m.id!=t.owner_id]
                if choices: t.owner_id=choices[0].id;t.version+=1
            session.add(Outbox(tenant_id=tid,key=key,kind='sla',task_id=t.id,version=t.version,
                              payload={'text':label+'，请进入工作台处理。'}))
            audit(session,tid,'worker','sla.escalation',t.id,{'threshold':threshold})

def deliver_notice(tid):
    with transaction(tid) as session:
        now=database_time(session)
        event=session.scalar(rows(session,Outbox,tid).where(Outbox.status.in_(['pending','retry']),Outbox.due_at<=now,Outbox.lease_until<now)
                             .order_by(Outbox.due_at).with_for_update(skip_locked=True).limit(1))
        if not event: return False
        task=locked(session,Task,tid,event.task_id) if event.task_id else None
        if not notification_current(task,event):
            event.status='cancelled';return True
        event.lease_until=now+45;event.attempts+=1
        if not session.scalar(rows(session,Notice,tid).where(Notice.outbox_id==event.id)):
            session.add(Notice(tenant_id=tid,outbox_id=event.id,task_id=event.task_id,text=event.payload['text'],kind=event.kind))
        config=company(session,tid)
        cipher=config.notification_cipher;options_cipher=config.notification_options_cipher;kind=config.notification_kind;identity=event.id;attempts=event.attempts;lease=event.lease_until
        notice_text=event.payload['text']
    success=False;error=''
    if cipher and kind in ('wecom','dingtalk'):
        with transaction(tid) as session:
            fresh=locked(session,Outbox,tid,identity)
            task=locked(session,Task,tid,fresh.task_id) if fresh.task_id else None
            if not notification_current(task,fresh):fresh.status='cancelled';fresh.lease_until=0;return True
        success,error=send_notification(kind,cipher,notice_text,options_cipher) if options_cipher else send_notification(kind,cipher,notice_text)
    with transaction(tid) as session:
        event=locked(session,Outbox,tid,identity)
        if event.attempts!=attempts or event.lease_until!=lease:return True
        task=locked(session,Task,tid,event.task_id) if event.task_id else None
        if not notification_current(task,event):event.status='cancelled';event.lease_until=0;return True
        config=company(session,tid)
        if config.notification_kind!=kind or config.notification_cipher!=cipher or config.notification_options_cipher!=options_cipher:
            event.status='retry';event.last_error='config_changed';event.due_at=database_time(session)+5;event.lease_until=0;return True
        if not cipher:
            event.status='local_only'
        elif success:
            event.status='delivered'
        else:
            event.status='failed' if attempts>=6 else 'retry'
            event.due_at=time.time()+min(300,2**attempts*5)
        event.last_error=error
        event.lease_until=0
    return True

class FlowCancelled(Exception):
    pass


def ai_guard(session,tid,job_id,token,lock_scope=False):
    job=locked(session,AIJob,tid,job_id)
    if not job or job.status!='leased' or job.lease_token!=token or job.lease_until<=database_time(session):
        raise FlowCancelled('suppressed_job')
    c=locked(session,Conversation,tid,job.conversation_id)
    if c.state not in ('AI_ACTIVE','HANDOFF_PENDING'):
        raise FlowCancelled('suppressed_human')
    tenant=(session.scalar(select(Tenant).where(Tenant.id==tid).with_for_update(read=True))
            if lock_scope else session.get(Tenant,tid))
    if not tenant.active:
        raise FlowCancelled('suppressed_channel')
    channel=(session.scalar(rows(session,Channel,tid).where(Channel.id==c.channel_id).with_for_update(read=True))
             if lock_scope and c.channel_id else session.get(Channel,(tid,c.channel_id)) if c.channel_id else None)
    if c.channel_id and (not channel or not channel.enabled):
        raise FlowCancelled('suppressed_channel')
    return job,c


def run_ai(tid,job_id,lease_token=None):
    with transaction(tid) as session:
        job=locked(session,AIJob,tid,job_id)
        if not job or job.status!='leased' or (lease_token is not None and job.lease_token!=lease_token):return
        token=job.lease_token;flow=ReceptionFlow(job.lease_until)
    flow.usage_id=None
    try:
        _run_ai(tid,job_id,token,flow)
    except (FlowExpired,FlowCancelled) as exc:
        reason='suppressed_timeout' if isinstance(exc,FlowExpired) else str(exc)
        with transaction(tid) as session:
            job=locked(session,AIJob,tid,job_id)
            if job and job.lease_token==token and job.status=='leased':
                if reason in ('suppressed_human','suppressed_channel'):
                    job.status='cancelled';job.lease_until=0
                else:job.lease_until=0  # durable bounded retry; no old answer is sent
            if flow.usage_id:
                session.get(Usage,(tid,flow.usage_id)).outcome=reason
    finally:
        if flow.usage_id:
            with transaction(tid) as session:
                usage=session.get(Usage,(tid,flow.usage_id))
                if usage:usage.detail={**usage.detail,'flow':flow.diagnostic()}


def _run_ai(tid,job_id,token,flow):
    started=time.monotonic()
    with flow.step('route'),transaction(tid) as session:
        job,c=ai_guard(session,tid,job_id,token)
        channel_id=c.channel_id
        current_message=session.get(Message,(tid,job.message_id))
        question=current_message.text
        intent=social_intent(question)
        injection=any(x in question.lower() for x in ['忽略规则','忽略之前','提示词','api密钥','api key','其他企业','其他租户','system prompt'])
        injection=injection or bool(re.search(r'系统提示|员工密码|内部资料|升级.{0,8}管理员|伪造.{0,15}(?:记录|联系)|关闭.{0,8}权限|未经审批',question))
        history=session.scalars(rows(session,Message,tid).where(Message.conversation_id==c.id,Message.sequence<=current_message.sequence).order_by(Message.sequence.desc()).limit(200)).all()
        history=[{'role':'user' if m.role=='visitor' else 'assistant','content':redacted_text(m.text)} for m in reversed(history)]
        from server.project_scope import project_history
        history=project_history(history)
        turn_plan=build_turn_plan(history)
        from .case_recommendations import select_for_message
        prepared_cards=[] if injection or intent else select_for_message(session,c,current_message,company(session,tid))
        if prepared_cards and 'cases' not in turn_plan['tasks']:turn_plan['tasks'].append('cases')
        while len(history)>3 and sum(len(m['content']) for m in history)>12000:history.pop(0)
        while history and history[0]['role']!='user':history.pop(0)
        query,clarification,route=resolve_question(question,history)
        if turn_plan['budget_status']=='unset' or len(turn_plan['tasks'])>1:query=turn_plan['retrieval_query']
        if turn_plan['direction']['status']=='undecided':clarification=None
        if injection:clarification=None;route='blocked'
        operation=None if injection else operational_reply(question)
        if operation and c.channel!='web':operation='如需专业顾问协助，可以直接发送“转人工”。'
        usage=Usage(tenant_id=tid,kind='retrieval',tokens=0,outcome='retrieving')
        usage.detail={'job_id':job.id,'message_id':job.message_id,'conversation_id':c.id,
                      'route':'social' if intent else 'operation_guidance' if operation else route,
                      'answer_policy':POLICY_VERSION,'scene':dialogue_context(history)['scene'],'history_messages':len(history)}
        session.add(usage);session.flush();flow.usage_id=usage.id
        company_name=session.get(Tenant,tid).name
    records=[];quick=None;retrieval_diagnostic={'embedding':'not_needed','rerank':'none'}
    if not (injection or clarification or intent or operation) and len(turn_plan['tasks'])<2 and turn_plan['direction']['status']!='undecided':
        with transaction(tid) as session:quick=quick_lookup(session,tid,channel_id,question,history)
    if quick:
        records=quick['records'];retrieval_diagnostic['backend']='reviewed_exact_faq'
    elif not (injection or clarification or intent or operation):
        with flow.step('retrieve'):
            records,retrieval_diagnostic=search_knowledge(tid,channel_id,query,checkpoint=flow.check,remaining=flow.remaining)
    if prepared_cards:
        from .knowledge_retrieval import PostgresScan
        with transaction(tid) as session:
            case_records=[r for r,_,_ in PostgresScan(session,tid,channel_id).scan(ids=[c['id'] for c in prepared_cards])]
        records=list({r['id']:r for r in records+case_records}.values())[:6]
    model_scope=retrieval_diagnostic
    with transaction(tid) as session:
        job,c=ai_guard(session,tid,job_id,token)
        records=revalidate(session,tid,records,channel_id)
        config_row=session.get(ModelConfig,(tid,'primary'))
        cfg={**model_gateway.DEFAULT,**config_row.config,'_tenant_id':tid};cipher=config_row.cipher
        config=company(session,tid);service_mode=config.service_mode
        if service_mode=='general_support':turn_plan={**turn_plan,'tasks':[]};prepared_cards=[]
        record_ids={r['id'] for r in records}
        turn_plan['case_evidence']=[{k:c[k] for k in ('id','title','summary','url')} for c in prepared_cards if c['id'] in record_ids]
        serialization_lock(session,tid,'ai-usage')
        day=datetime.fromtimestamp(time.time(),ZoneInfo(config.timezone)).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        calls=session.scalar(select(func.count()).select_from(Usage).where(Usage.tenant_id==tid,Usage.kind=='llm',Usage.created_at>=day))
        tokens=session.scalar(select(func.coalesce(func.sum(Usage.tokens),0)).where(Usage.tenant_id==tid,Usage.kind=='llm',Usage.created_at>=day))
        reservation=sum(len(k['content']) for k in records)+sum(len(m['content']) for m in history)+cfg['max_output_tokens']+3000
        eligible=bool(cfg['enabled'] and cipher and not injection and not clarification and not operation and not intent and not quick and model_scope.get('model_scope')!='unavailable')
        health=model_health(session,tid,model_gateway.fingerprint(cfg),database_time(session)) if eligible else {'state':'not_needed','allow_call':False}
        budget_ok=calls<config.daily_ai_limit and tokens+reservation<=config.daily_token_limit
        call=eligible and budget_ok and health['allow_call']
        usage=session.get(Usage,(tid,flow.usage_id));usage.kind='llm' if call else 'faq' if quick else 'retrieval';usage.tokens=reservation if call else 0;usage.outcome='reserved'
        usage.detail={**usage.detail,'retrieval':retrieval_diagnostic,'model_fingerprint':model_gateway.fingerprint(cfg),
                      'model':cfg.get('model',''),'retrieved':[evidence_summary(k) for k in records],'budget_blocked':eligible and not budget_ok,
                      'model_health':health,'customer_model_wait_seconds':min(cfg['timeout'],CUSTOMER_WAIT_SECONDS)}
        fingerprint=model_gateway.fingerprint(cfg)
    answer=quick['answer'] if quick else social_fallback(intent,history) if intent else None
    used=quick['records'] if quick else []
    outcome='faq' if quick else 'social_fast' if intent else 'fallback_circuit_open' if eligible and not health['allow_call'] else 'retrieval';provider_diagnostic={}
    if call:
        try:
            with flow.step('generate'):
                cfg={**cfg,'timeout':min(cfg['timeout'],CUSTOMER_WAIT_SECONDS,flow.remaining()),'_turn_plan':turn_plan}
                cfg['api_key']=model_gateway.protect(cipher,decrypt=True)
                if intent:answer=conversational_answer(cfg,company_name,history,intent)
                else:
                    answer,used=(model_gateway.grounded_answer(cfg,company_name,history,records,service_mode=service_mode)
                                 if service_mode=='general_support' else model_gateway.grounded_answer(cfg,company_name,history,records))
                outcome='llm'
        except model_gateway.ProviderError as exc:
            provider_diagnostic={k:v for k,v in exc.diagnostic.items() if (k=='finish_reason' and v in ('stop','length','content_filter','tool_calls','insufficient_system_resource','aborted','unknown')) or (k in ('prompt_tokens','completion_tokens') and isinstance(v,int) and 0<=v<=1_000_000)}
            if re.fullmatch(r'http_[1-5][0-9]{2}|timeout|connection|empty|length|provider_error',exc.code):
                provider_diagnostic['error_code']=exc.code
            # Bounded diagnostic labels only: never persist provider text or credentials.
            outcome={
                '模型未返回受支持的回答格式':'fallback_format',
                '模型回答结构不正确':'fallback_structure',
                '模型回答缺少有效知识依据':'fallback_citations',
                '模型引入了未核实数字':'fallback_numbers',
                '模型引入了未核实日期组合':'fallback_numbers',
                '模型回答涉及未授权承诺或操作':'fallback_claims',
                '模型回答重复或偏离本轮需求':'fallback_quality',
            }.get(str(exc),{'empty':'fallback_empty','length':'fallback_length','timeout':'fallback_timeout','connection':'fallback_connection',
                            'http_401':'fallback_auth','http_403':'fallback_auth','http_402':'fallback_quota','http_429':'fallback_quota'}.get(exc.code,'fallback_provider'))
        except HTTPException: outcome='fallback_provider'
    with transaction(tid) as session:
        job,c=ai_guard(session,tid,job_id,token,lock_scope=True)
        with flow.step('verify'):
            # Recheck every input evidence, including material the model did not cite.
            active=revalidate(session,tid,records,channel_id,lock=True)
            active_by={r['id']:r for r in active}
            current=session.scalar(rows(session,ModelConfig,tid).where(ModelConfig.id=='primary').with_for_update(read=True))
            model_valid=current.config.get('enabled') and current.cipher==cipher and model_gateway.fingerprint({**model_gateway.DEFAULT,**current.config})==fingerprint
            valid=answer and (quick_current(session,tid,quick) if quick else bool(intent) or model_valid)
            valid=valid and len(active)==len(records) and all(k['id'] in active_by and k==active_by[k['id']] for k in used)
            if not valid:
                if answer:outcome='fallback_changed'
                records=active
                if intent:
                    answer=social_fallback(intent,history);used=[];outcome='conversation_fallback'
                elif operation or clarification:
                    answer=operation or clarification;used=[];outcome='operation_guidance' if operation else 'clarification'
                elif records:answer,used=support_fallback(records,query) if service_mode=='general_support' else contextual_fallback(records,history)
                else:
                    if injection:
                        answer='这个细节需要专业顾问进一步确认。您可以补充具体使用场景，也可以转人工，我会保留当前需求供顾问查看。';used=[]
                    elif service_mode=='general_support':
                        answer='目前没有找到这个完整型号的已发布资料。请核对型号，或告诉我您想了解的功能，我也可以帮您联系顾问。' if model_scope.get('model_scope')=='unavailable' else '可以先告诉我产品型号或具体问题，我会根据本企业已确认资料为您解答。'
                        used=[]
                    else:answer,used=contextual_fallback([],history)
                    category='知识范围或特殊承诺待核实'
                    gap=session.scalar(rows(session,KnowledgeGap,tid).where(KnowledgeGap.category==category,KnowledgeGap.status=='open'))
                    if gap:gap.count+=1
                    else:session.add(KnowledgeGap(tenant_id=tid,category=category))
        with flow.step('deliver'):
            effective_plan={**turn_plan,'case_evidence':[c for c in turn_plan['case_evidence'] if c['id'] in active_by]}
            if not injection and not operation and not clarification and service_mode!='general_support':
                incomplete=coverage_issue(answer,effective_plan)
                if incomplete or (not valid or outcome.startswith('fallback')) and effective_plan['tasks']:
                    recovery=task_fallback(effective_plan,active)
                    if recovery:
                        answer,used=recovery
                        if incomplete and valid:outcome='faq' if quick else 'fallback_quality'
            config_for_cases=session.scalar(rows(session,CompanySettings,tid).where(CompanySettings.id=='primary').with_for_update(read=True))
            case_cards=[] if injection else select_for_message(session,c,current_message,config_for_cases)
            expected={(card['id'],card['version'],card['source_digest']) for card in prepared_cards}
            used_ids={r['id'] for r in used}
            case_cards=[card for card in case_cards if (card['id'],card['version'],card['source_digest']) in expected and card['id'] in used_ids]
            reply=add_message(session,c,'ai',answer,citations=[citation(k) for k in used])
            reply.case_recommendations=case_cards
            job.status='done';job.lease_until=0
            usage=session.get(Usage,(tid,flow.usage_id));usage.outcome=outcome;usage.latency_ms=round((time.monotonic()-started)*1000)
            usage.detail={**usage.detail,'used':[evidence_summary(k) for k in used],'provider_diagnostic':provider_diagnostic,
                          'case_recommendations':[{'id':k['id'],'version':k['version'],'for_sequence':k['for_sequence']} for k in case_cards],
                          'turn_plan':{'version':turn_plan['version'],'tasks':turn_plan['tasks'],
                                       'direction_status':turn_plan['direction']['status'],
                                       'case_ids':[c['id'] for c in effective_plan['case_evidence']],
                                       'coverage_issue':coverage_issue(answer,effective_plan)},
                          'evidence_check':{'input_count':len(active_by),'withdrawn_or_changed':len(active_by)!=len(usage.detail['retrieved'])}}


def lease_ai(tid,with_token=False):
    with transaction(tid) as session:
        now=database_time(session);config=company(session,tid)
        serialization_lock(session,tid,'ai-lease')
        ongoing=session.scalar(select(func.count()).select_from(AIJob).where(AIJob.tenant_id==tid,AIJob.status=='leased',AIJob.lease_until>now))
        if ongoing>=config.ai_concurrency: return None
        older=aliased(AIJob);older_message=aliased(Message)
        unfinished=exists(select(1).select_from(older).join(older_message,(older_message.tenant_id==older.tenant_id)&(older_message.id==older.message_id))
                          .where(older.tenant_id==tid,older.conversation_id==AIJob.conversation_id,older.status.in_(['pending','leased']),older_message.sequence<Message.sequence))
        job=session.scalar(rows(session,AIJob,tid).join(Message,(Message.tenant_id==AIJob.tenant_id)&(Message.id==AIJob.message_id))
                          .where(AIJob.status.in_(['pending','leased']),AIJob.lease_until<now,~unfinished)
                          .order_by(AIJob.created_at,Message.sequence).with_for_update(of=AIJob,skip_locked=True).limit(1))
        if not job: return None
        if job.attempts>=3:
            c=locked(session,Conversation,tid,job.conversation_id)
            if c.state in ('AI_ACTIVE','HANDOFF_PENDING'):
                add_message(session,c,'ai','这条咨询暂时未能完成处理，您可以继续补充需求或联系专业顾问。')
            job.status='failed';return None
        job.status='leased';job.lease_until=now+120;job.attempts+=1;job.lease_token=uid()
        return (job.id,job.lease_token) if with_token else job.id

def sync_source(tid,source_id):
    with transaction(tid) as session:
        source=session.get(KnowledgeSource,(tid,source_id));url=source.url;allowed=company(session,tid).source_hosts
    try:
        content,digest=fetch_public(url,allowed)
        with transaction(tid) as session:
            source=locked(session,KnowledgeSource,tid,source_id)
            if source.digest!=digest:
                serialization_lock(session,tid,'knowledge:'+source.id)
                previous=session.scalar(rows(session,Knowledge,tid).where(Knowledge.family==source.id).order_by(Knowledge.version.desc()).limit(1))
                session.add(Knowledge(tenant_id=tid,family=source.id,version=previous.version+1 if previous else 1,
                                      title=source.title,content=content,source_url=url,author_id='source-sync',risk=risk(content),digest=digest))
                source.digest=digest;source.last_status='review_required'
                audit(session,tid,'worker','knowledge.sync_candidate',source.id)
            else: source.last_status='unchanged'
    except Exception as exc:
        with transaction(tid) as session:
            source=locked(session,KnowledgeSource,tid,source_id)
            source.last_status='failed_tls' if 'CERTIFICATE_VERIFY_FAILED' in str(exc) else 'failed'
            audit(session,tid,'worker','knowledge.sync_failed',source.id)

def lease_source(tid):
    with transaction(tid) as session:
        source=session.scalar(rows(session,KnowledgeSource,tid).where(KnowledgeSource.active==True,KnowledgeSource.next_at<=time.time())
                              .order_by(KnowledgeSource.next_at).with_for_update(skip_locked=True).limit(1))
        if not source: return None
        source.next_at=time.time()+source.interval_hours*3600
        source.last_status='syncing'
        return source.id

def heartbeat():
    with transaction() as session:
        row=session.get(WorkerState,WORKER_ID)
        if row: row.heartbeat_at=time.time()
        else: session.add(WorkerState(id=WORKER_ID,heartbeat_at=time.time()))

def enforce_retention(tid):
    with transaction(tid) as session:
        config=company(session,tid);cutoff=time.time()-config.retention_days*86400
        conversations=session.scalars(rows(session,Conversation,tid).where(Conversation.created_at<cutoff,Conversation.state=='CLOSED',Conversation.name!='已匿名化访客').limit(100).with_for_update(skip_locked=True)).all()
        for c in conversations:
            c.name='已匿名化访客';c.profile={}
            for m in session.scalars(rows(session,Message,tid).where(Message.conversation_id==c.id)):
                m.text='[保留期限已届满]';m.citations=[];m.case_recommendations=[]
                if m.role=='visitor':m.actor_name=''
            lead=session.scalar(rows(session,Lead,tid).where(Lead.conversation_id==c.id))
            if lead:lead.name='已匿名化访客';lead.phone_cipher=''
            thread=session.scalar(rows(session,ExternalThread,tid).where(ExternalThread.conversation_id==c.id))
            if thread:thread.route_cipher='';thread.eligible_until=0
            audit(session,tid,'worker','privacy.retention',c.id)

def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    def stop(*_):STOP.set();WAKE.set()
    signal.signal(signal.SIGINT,stop)
    signal.signal(signal.SIGTERM,stop)
    listener=PgListener(JOB_CHANNEL,lambda _:WAKE.set());listener.start()
    active={};last=0;last_retention=0;rotation=0;last_maintenance=0
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        while not STOP.is_set():
            WAKE.clear()
            try:
                maintenance_due=time.monotonic()-last_maintenance>=1
                if maintenance_due:heartbeat();last_maintenance=time.monotonic()
                for key,future in list(active.items()):
                    if future.done():
                        if future.exception():log.error('Background task failed; durable lease recovery will retry.')
                        active.pop(key)
                tenants=tenant_ids()
                if tenants:
                    rotation=(rotation+6)%len(tenants)
                    tenants=tenants[rotation:]+tenants[:rotation]
                for tid in tenants:
                    for key,future in list(active.items()):
                        if future.done():
                            if future.exception():log.error('Background task failed; lease recovery will retry.')
                            active.pop(key)
                    if maintenance_due:tick_sla(tid)
                    if time.time()-last_retention>3600:enforce_retention(tid)
                    if len(active)<5:
                        attempt=lease_ai(tid,with_token=True)
                        if attempt:
                            future=pool.submit(run_ai,tid,*attempt);future.add_done_callback(lambda _:WAKE.set())
                            active[('ai',attempt[0])]=future;WAKE.set()
                    if ('notice',tid) not in active and len(active)<6: active[('notice',tid)]=pool.submit(deliver_notice,tid)
                    if time.time()-last>30 and len(active)<5:
                        source=lease_source(tid)
                        if source: active[('source',source)]=pool.submit(sync_source,tid,source)
                if time.time()-last>30: last=time.time()
                if time.time()-last_retention>3600:last_retention=time.time()
            except Exception: log.error('Worker cycle failed; retrying without exposing payloads.')
            WAKE.wait(timeout=1)
    listener.close()

if __name__=='__main__': main()
