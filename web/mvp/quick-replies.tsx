import {useEffect,useState} from 'react';
type D=Record<string,any>;
export function QuickReplyEditor({knowledge,canPublish,api,onSaved}:{knowledge:D;canPublish:boolean;api:(path:string,method?:string,body?:unknown)=>Promise<any>;onSaved:(value:D)=>void}){
 const [questions,sq]=useState(''),[answer,sa]=useState(''),[confirmed,sc]=useState(false),[busy,sb]=useState(false),[error,se]=useState('');
 useEffect(()=>{sq((knowledge.quick_reply?.questions||[]).join('\n'));sa(knowledge.quick_reply?.answer||knowledge.content);sc(false);se('')},[knowledge.id,knowledge.quick_reply]);
 if(knowledge.status!=='published'||knowledge.visibility!=='public'||knowledge.category==='文档知识')return null;
 async function save(enabled:boolean){sb(true);se('');try{const value=await api('/quick-replies/'+knowledge.id,'PUT',{version:knowledge.version,enabled,questions:questions.split('\n'),answer,confirmed});onSaved(value);sc(false)}catch(e){se((e as Error).message)}finally{sb(false)}}
 return <details className="quick-answer-editor"><summary>标准问题快捷回答 · {knowledge.quick_reply?.enabled?'已启用':'未启用'}</summary><p className="footnote">适用于公司介绍、准备资料等明确标准问题。精确匹配才直接回答；来源撤回、过期或版本变化后停止使用。个性化需求继续分析。</p>
 <label>可直接回答的问题（每行一个）<textarea rows={3} maxLength={4200} value={questions} disabled={!canPublish||busy} onChange={e=>sq(e.target.value)}/></label>
 <label>对客户展示的答案<textarea rows={4} maxLength={1000} value={answer} disabled={!canPublish||busy} onChange={e=>sa(e.target.value)}/></label>
 {canPublish&&<><label className="inline-check"><input type="checkbox" checked={confirmed} onChange={e=>sc(e.target.checked)}/>已核对问题、适用范围和来源，答案可直接对客户展示</label><div className="mvp-actions"><button type="button" className="btn primary" disabled={busy||!confirmed||!questions.trim()||!answer.trim()} onClick={()=>void save(true)}>审核并启用快捷回答</button>{knowledge.quick_reply?.enabled&&<button type="button" className="btn" disabled={busy} onClick={()=>void save(false)}>停用快捷回答</button>}</div></>}
 {error&&<p className="error-text" role="alert">{error}</p>}</details>
}
