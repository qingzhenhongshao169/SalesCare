import {useEffect, useState} from 'react';
import {ArrowUpRight, BookOpen} from 'lucide-react';
import {safeCaseUrl, visibleCaseCards} from './caseCards.mjs';

type D = Record<string, any>;
type Api = (path: string, method?: string, body?: unknown) => Promise<any>;

export function CaseCards({items}: {items: unknown}) {
  const cards = visibleCaseCards(items);
  if (!cards.length) return null;
  return <section className="case-recommendations" aria-label="相关项目案例">
    <div className="case-section-title"><BookOpen size={15}/><span>相关项目经验</span></div>
    {cards.map(card => <article className="case-card" key={card.id}>
      <span className="case-card-label">企业历史案例 · 能力参考</span>
      <h3>{card.title}</h3><p>{card.summary}</p><p className="case-match-reason">{card.reason}</p>
      <a href={safeCaseUrl(card.url)!} target="_blank" rel="noopener noreferrer">查看官网案例<ArrowUpRight size={15}/></a>
    </article>)}
    <p className="case-disclaimer">历史案例供能力参考，具体范围、费用和交期需另行评估。</p>
  </section>;
}

export function CaseEditor({knowledge: k, canPublish, api, onSaved}: {
  knowledge: D; canPublish: boolean; api: Api; onSaved: (value: D) => void;
}) {
  const [settings, setSettings] = useState<D|null>(null);
  const [form, setForm] = useState<D>({});
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    const value = k.case_recommendation || {};
    setForm({summary: value.summary || '', service_types: value.service_types || [],
      industries: value.industries || [], goals: value.goals || [], priority: value.priority ?? 50});
    setConfirmed(false); setError('');
  }, [k.id, k.case_recommendation]);
  useEffect(() => {
    let alive = true;
    if (canPublish) void api('/case-recommendations/settings').then(v => {if (alive) setSettings(v);}).catch(() => {});
    return () => {alive = false;};
  }, [canPublish]);
  if (k.visibility !== 'public' || k.category === '文档知识' || !canPublish) return null;
  async function save(enabled: boolean) {
    setBusy(true); setError('');
    try {onSaved(await api('/case-recommendations/' + k.id, 'PUT', {version: k.version, enabled, confirmed, ...form}));}
    catch (e) {setError((e as Error).message);} finally {setBusy(false);}
  }
  return <details className="case-editor"><summary>项目案例推荐{k.case_recommendation?.enabled ? ' · 已启用' : ''}</summary>
    <p className="muted">先发布案例知识，再选择与原文一致的分类和简介。按用户需求与交流进度展示，不代表当前项目报价。</p>
    {k.status !== 'published' ? <p>此案例尚未发布，审核发布后可配置推荐。</p> : <>
      <label>案例简介（从正文中选取连续原文）<textarea rows={3} maxLength={600} value={form.summary || ''}
        disabled={busy} onChange={e => setForm({...form, summary: e.target.value})}/></label>
      {settings && (['service_types', 'industries', 'goals'] as const).map(key => <fieldset key={key}>
        <legend>{{service_types: '服务类型', industries: '适用行业', goals: '功能与目标'}[key]}</legend>
        <div className="case-tag-options">{Object.entries(settings[key] as Record<string, string>).map(([id, label]) => <label className="inline-check" key={id}>
          <input type="checkbox" checked={(form[key] || []).includes(id)} disabled={busy} onChange={e => setForm({...form,
            [key]: e.target.checked ? [...(form[key] || []), id] : (form[key] || []).filter((v: string) => v !== id)})}/>{label}</label>)}</div>
      </fieldset>)}
      <label>运营优先级（0–100，仅在相关案例之间排序）<input type="number" min={0} max={100} value={form.priority ?? 50}
        disabled={busy} onChange={e => setForm({...form, priority: Number(e.target.value)})}/></label>
      <label className="inline-check"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)}/>
        已核对案例原文、分类及官网来源，可以用于对客展示</label>
      <div className="mvp-actions"><button type="button" className="btn primary" disabled={busy || !confirmed || !form.service_types?.length || !form.summary?.trim()}
        onClick={() => void save(true)}>审核并启用案例推荐</button>
        {k.case_recommendation?.enabled && <button type="button" className="btn" disabled={busy} onClick={() => void save(false)}>停用案例推荐</button>}</div>
    </>}{error && <p className="error-text" role="alert">{error}</p>}
  </details>;
}

export function CasePolicyEditor({api, canEdit}: {api: Api; canEdit: boolean}) {
  const [form, setForm] = useState<D|null>(null), [error, setError] = useState(''), [notice, setNotice] = useState('');
  useEffect(() => {void api('/case-recommendations/settings').then(v => setForm(v.policy)).catch(e => setError(e.message));}, []);
  if (!form) return error ? <p className="error-text">{error}</p> : null;
  async function save() {
    setError(''); setNotice('');
    try {await api('/case-recommendations/settings', 'PUT', form); setNotice('案例推荐规则已保存');}
    catch (e) {setError((e as Error).message);}
  }
  return <details className="case-editor"><summary>案例推荐时机与权重</summary>
    <p className="muted">只有当前在询问相关方案或公司能力时，才考虑展示已审核的相关案例。交流轮数只是最低门槛；预算更正、费用与排期沟通不会单独触发推荐。主动展示后至少间隔3条有效业务消息，主动询问案例可提前展示。</p>
    <label className="inline-check"><input type="checkbox" disabled={!canEdit} checked={form.enabled}
      onChange={e => setForm({...form, enabled: e.target.checked})}/>开启历史案例推荐</label>
    <label>主动展示前至少交流几条有效业务消息<input type="number" min={2} max={6} disabled={!canEdit} value={form.min_business_turns}
      onChange={e => setForm({...form, min_business_turns: Number(e.target.value)})}/></label>
    <label>每次最多推荐几个案例<input type="number" min={1} max={2} disabled={!canEdit} value={form.max_items}
      onChange={e => setForm({...form, max_items: Number(e.target.value)})}/></label>
    <div className="case-weight-grid">{Object.entries({service: '服务类型', goals: '功能与目标', industry: '行业', priority: '运营优先级'}).map(([key, label]) =>
      <label key={key}>{label}权重<input type="number" min={0} max={100} disabled={!canEdit} value={form.weights[key]}
        onChange={e => setForm({...form, weights: {...form.weights, [key]: Number(e.target.value)}})}/></label>)}</div>
    <p className="muted">四项权重总和须为100，服务类型权重大于0。</p>
    {canEdit && <button className="btn" type="button" onClick={() => void save()}>保存推荐规则</button>}
    {error && <p role="alert" className="error-text">{error}</p>}{notice && <p role="status">{notice}</p>}
  </details>;
}

export function CaseCatalogueImport({api, onImported}: {api: Api; onImported: () => void}) {
  const [busy, setBusy] = useState(false), [notice, setNotice] = useState('');
  async function load() {
    setBusy(true);
    try {const result = await api('/case-catalogue/import', 'POST', {apply: true});
      setNotice('已导入' + result.drafts.length + '条待审案例；已有资料保留。发布并启用后才会推荐。');onImported();}
    catch (e) {setNotice((e as Error).message);} finally {setBusy(false);}
  }
  return <div className="case-catalogue-import"><button className="btn" type="button" disabled={busy} onClick={() => void load()}>导入标梵官网案例（待审核）</button>
    {notice && <p role="status" className="muted">{notice}</p>}</div>;
}
