import React from 'react';
type D=Record<string,any>;
export const queueLabels:D={all:'全部会话',mine:'我的接待',waiting:'待接待',first_reply:'待首回复',overdue:'超时待处理',appointment:'预约未到时',followup:'跟进中',ai:'AI接待',closed:'已结束'};
export const deliveryLabels:D={pending:'待发送',retry:'正在重试',delivered:'服务商已接受',local_only:'仅站内提醒',failed:'发送失败',cancelled:'已过期',unknown:'状态待核对'};
const errorLabels:D={rate_limited:'服务商限流',provider_http:'服务商请求失败',provider_rejected:'服务商未接受',timeout:'请求超时',network_or_format:'连接或格式异常',invalid_destination:'通知地址配置异常',invalid_response:'回执格式异常',configuration_error:'通知配置异常',config_changed:'配置变化，等待重试'};
const outcomeLabels:D={llm:'大模型回答',retrieval:'知识简答',clarification:'澄清咨询主题',operation_guidance:'引导页面操作',conversation_fallback:'简短对话回应',fallback_format:'输出格式校验失败，已回退',fallback_structure:'回答结构校验失败，已回退',fallback_citations:'知识引用校验失败，已回退',fallback_numbers:'数字依据校验失败，已回退',fallback_claims:'承诺校验失败，已回退',fallback_provider:'模型调用失败，已回退',fallback_empty:'模型返回空内容，已回退',fallback_length:'模型输出达到长度限制，已回退',fallback_timeout:'模型超时，已回退',fallback_connection:'模型连接异常，已回退',fallback_auth:'模型认证失败，已回退',fallback_quota:'服务商限流或额度不足，已回退',fallback_changed:'资料或模型配置变化，已回退',suppressed_human:'人工已接管，取消AI投递',suppressed_job:'任务已变化，取消AI投递'};
export function QueueOverview({summary}:{summary:D}){
  return <section className="reception-overview" aria-label="接待待办统计">{['waiting','first_reply','overdue','appointment'].map(key=><div className={'panel reception-stat '+key} key={key}><span>{queueLabels[key]}</span><strong>{summary.counts?.[key]??'—'}</strong></div>)}{(summary.owner_attention>0||summary.worker_ready===false)&&<p className="reception-warning">{summary.owner_attention>0&&`${summary.owner_attention}条已接待会话的顾问连接已离线，请检查并按需转派。`}{summary.worker_ready===false&&'跟进任务进程未就绪，请联系管理员检查。'}</p>}</section>
}
export function QueueFilter({value,change}:{value:string,change:(v:string)=>void}){
  return <label className="queue-filter">接待状态<select aria-label="接待状态" value={value} onChange={e=>change(e.target.value)}>{Object.entries(queueLabels).map(([key,label])=><option value={key} key={key}>{label}</option>)}</select></label>
}
export function HandoffPanel({conversation}:{conversation:D|null}){
  const summary=conversation?.handoff_summary;
  if(!summary)return null;
  return <section className="handoff-context"><h3>接待摘要</h3><p className="footnote">来自客户原话和已发送消息，可点击核对。</p>{summary.confirmed_needs?.map((item:D)=><div key={item.label}><strong>{item.label}：{item.display}</strong><p className="summary-evidence">{item.evidence}</p></div>)}{summary.latest_question&&<div><span className="muted">客户最近的问题</span><a href={'#message-'+summary.latest_question.message_id}>{summary.latest_question.text}</a></div>}{summary.latest_reply&&<div><span className="muted">最近已发送的回复</span><a href={'#message-'+summary.latest_reply.message_id}>{summary.latest_reply.text}</a></div>}{summary.awaiting_reply&&<p className="reception-warning">客户的新消息尚无后续回复。</p>}</section>
}
export function AnswerDiagnostics({items}:{items:D[]}){
  if(!items?.length)return null;
  return <details className="answer-diagnostics"><summary>回答记录 · {items.length}条</summary>{items.map(item=><div key={item.id}><strong>{outcomeLabels[item.outcome]||'处理记录'}</strong><p>{item.latency_ms}毫秒{item.detail?.budget_blocked?' · 已达到本企业调用额度':''}</p><small>检索资料{item.detail?.retrieved?.length||0}条 · 实际引用{item.detail?.used?.length||0}条</small></div>)}</details>
}
export function ReminderHistory({data,open}:{data:D,open:(id:string)=>void}){
  return <section className="panel mvp-card"><h2>员工提醒发送记录</h2><p className="muted">{data.configured?'已配置员工通知渠道。':'当前仅站内提醒，配置企业渠道后再测试真实送达。'}服务商接受不代表员工已读或客户已被联系。</p>{data.items?.length?data.items.map((event:D)=><div className="mvp-notice" key={event.id}><div><strong>{deliveryLabels[event.status]||'状态待核对'}</strong><p className="footnote">{event.kind==='sla'?'联系时限提醒':event.kind==='transfer'?'转派提醒':event.kind==='test'?'连接测试':'新接待提醒'} · 尝试{event.attempts}次{event.last_error?' · '+(errorLabels[event.last_error]||'配置待检查'):''}{!event.current?' · 原任务已变化':''}</p></div>{event.conversation_id&&<button type="button" className="btn compact" onClick={()=>open(event.conversation_id)}>查看会话</button>}</div>):<p className="muted">尚无提醒发送记录。</p>}</section>
}
