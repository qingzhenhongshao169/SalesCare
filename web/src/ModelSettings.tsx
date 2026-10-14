import {useEffect, useState} from 'react';
import {CheckCheck, KeyRound, Loader2, PlugZap, Save, ShieldCheck} from 'lucide-react';

type Data = Record<string,any>;
type Props = {api:(path:string,method?:string,body?:unknown)=>Promise<any>, onChanged:()=>Promise<void>};
const defaults:Data={protocol:'openai',base_url:'https://api.openai.com/v1',model:'',api_key:'',enabled:false,timeout:20,max_output_tokens:800,token_parameter:'max_tokens',system_role:'system'};
const protocols=[['openai','OpenAI 兼容 · Chat Completions'],['responses','OpenAI · Responses'],['anthropic','Anthropic · Messages'],['gemini','Google · Gemini']];
const roots:Data={openai:'https://api.openai.com/v1',responses:'https://api.openai.com/v1',anthropic:'https://api.anthropic.com/v1',gemini:'https://generativelanguage.googleapis.com/v1beta'};

export default function ModelSettings({api,onChanged}:Props){
  const [form,setForm]=useState<Data>({...defaults});
  const [saved,setSaved]=useState<Data|null>(null);
  const [busy,setBusy]=useState('');
  const [dirty,setDirty]=useState(false);
  const [error,setError]=useState('');
  const [result,setResult]=useState('');
  const consoleAddress=/^https?:\/\/platform\.deepseek\.com(?:\/|$)/i.test(form.base_url);
  const deepseekAddress=/^https:\/\/api\.deepseek\.com(?:\/|$)/i.test(form.base_url);
  useEffect(()=>{let live=true;api('/admin/model').then(c=>{if(live){setSaved(c);setForm({...c,api_key:''})}}).catch(e=>{if(live)setError(e.message)});return()=>{live=false}},[]);
  function change(key:string,value:any){setForm(f=>({...f,[key]:value}));setDirty(true);setResult('');setError('')}
  function selectProtocol(protocol:string){setForm(f=>({...f,protocol,base_url:roots[protocol],model:'',api_key:'',enabled:false}));setDirty(true);setResult('');setError('')}
  async function save(enabled=false){const c=await api('/admin/model','PUT',{...form,enabled});setSaved(c);setForm({...c,api_key:''});setDirty(false);await onChanged();return c}
  async function action(kind:string){
    if(busy)return;setBusy(kind);setError('');setResult('');
    try{
      if(kind==='save'){await save(false);setResult('配置已保存。测试连接成功后，可启用客服模型。')}
      if(kind==='test'){await save(false);const r=await api('/admin/model/test','POST');const c=await api('/admin/model');setSaved(c);setForm({...c,api_key:''});setResult(`连接成功 · ${r.latency_ms} ms。尚未启用，请确认后启用。`)}
      if(kind==='toggle'){if(dirty)throw new Error('配置有修改，请先保存并测试连接');await save(!saved?.enabled);setResult(saved?.enabled?'模型已停用，继续使用企业知识回答。':'模型已启用，回答将基于本企业已发布知识。')}
      if(kind==='delete'){const c=await api('/admin/model/key','DELETE');setSaved(c);setForm({...c,api_key:''});setDirty(false);await onChanged();setResult('密钥已移除，模型已停用。')}
    }catch(e){setError((e as Error).message)}finally{setBusy('')}
  }
  return <div className="page model-page"><div className="page-heading"><div><div className="eyebrow">MODEL CONNECTION</div><h1>让模型理解，让知识作答</h1><p>当前企业独立配置。先保存和测试，确认后再启用。</p></div><span className={`tag ${saved?.enabled?'green':'neutral'}`}>{saved?.enabled?'模型已启用':'知识库回答中'}</span></div>
    <div className="model-layout"><section className="panel model-panel"><div className="panel-heading"><div><h2><PlugZap size={20}/>模型连接</h2><p>仅企业管理者可以查看和修改</p></div><ShieldCheck size={20}/></div>
      <form className="form model-form" onSubmit={e=>{e.preventDefault();action('save')}}>
        <label>接口协议<select aria-label="接口协议" value={form.protocol} disabled={!!busy} onChange={e=>selectProtocol(e.target.value)}>{protocols.map(([id,name])=><option key={id} value={id}>{name}</option>)}</select></label>
        <label>API 服务根地址<input aria-label="API 服务根地址" required type="url" value={form.base_url} disabled={!!busy} onChange={e=>change('base_url',e.target.value)} placeholder="https://api.example.com/v1"/><small>填写服务商提供的 Base URL，系统自动追加请求路径。请勿把密钥放入地址。</small></label>
        {consoleAddress&&<p className="form-error" role="alert">当前填写的是 DeepSeek 管理控制台地址。API 地址应为 https://api.deepseek.com；更换地址后请重新填写密钥。</p>}
        <label>模型名称<input aria-label="模型名称" value={form.model} maxLength={150} disabled={!!busy} onChange={e=>change('model',e.target.value)} placeholder="填写服务商控制台中的准确模型名称或接入点 ID"/><small>按账号实际开通的模型填写，避免使用过期的型号示例。</small></label>
        {(deepseekAddress||consoleAddress)&&<small>DeepSeek 当前官方型号示例：deepseek-flash、deepseek-v4-pro。请按账号可用型号填写，不能只填 deepseek。</small>}
        <label>API 密钥<span className="key-state"><KeyRound size={14}/>{saved?.has_key?'已加密保存；留空保持原密钥':'尚未配置，可先保存其他设置'}</span><input aria-label="API 密钥" type="password" autoComplete="new-password" value={form.api_key} maxLength={4096} disabled={!!busy} onChange={e=>change('api_key',e.target.value)} placeholder={saved?.has_key?'如需替换，请输入新密钥':'之后在这里填写密钥'}/><small>不回显已保存密钥，不保存到浏览器。更换协议或地址后，需要重新填写密钥。</small></label>
        <div className="model-field-row"><label>请求超时（秒）<input aria-label="请求超时" type="number" min={5} max={45} value={form.timeout} disabled={!!busy} onChange={e=>change('timeout',Number(e.target.value))}/></label><label>最大输出 Token<input aria-label="最大输出 Token" type="number" min={128} max={4000} value={form.max_output_tokens} disabled={!!busy} onChange={e=>change('max_output_tokens',Number(e.target.value))}/></label></div>
        {form.protocol==='openai'&&<details className="model-advanced"><summary>兼容参数</summary><div className="model-field-row"><label>输出限制参数<select aria-label="输出限制参数" value={form.token_parameter} onChange={e=>change('token_parameter',e.target.value)}><option value="max_tokens">max_tokens</option><option value="max_completion_tokens">max_completion_tokens</option></select></label><label>系统消息角色<select aria-label="系统消息角色" value={form.system_role} onChange={e=>change('system_role',e.target.value)}><option value="system">system</option><option value="developer">developer</option></select></label></div></details>}
        {error&&<p className="form-error" role="alert">{error}</p>}{result&&<div className="model-result" role="status"><CheckCheck size={18}/>{result}</div>}
        <div className="model-actions"><button className="btn" disabled={!!busy}>{busy==='save'?<Loader2 className="spin" size={16}/>:<Save size={16}/>}保存配置</button><button type="button" className="btn" disabled={!!busy||!form.model||(!form.api_key&&!saved?.has_key)} onClick={()=>action('test')}>{busy==='test'?<Loader2 className="spin" size={16}/>:<PlugZap size={16}/>}测试连接</button><button type="button" className="btn primary" disabled={!!busy||dirty||(!saved?.enabled&&!saved?.tested_fingerprint)} onClick={()=>action('toggle')}>{saved?.enabled?'停用模型':'启用模型'}</button></div>
        {saved?.has_key&&<button type="button" className="text-btn model-key-remove" disabled={!!busy} onClick={()=>action('delete')}>移除密钥并停用</button>}
      </form></section>
      <aside className="model-help"><section className="panel"><span className="eyebrow">COMPATIBILITY</span><h2>按协议连接服务商</h2><p>OpenAI 兼容协议可连接 DeepSeek、千问、GLM 等提供兼容接口的模型服务。Claude 和 Gemini 也有独立协议。</p><p>其他服务商可填写自定义 Base URL 与模型名称。账号地域、权限、参数和模型能力仍需测试确认。</p><div className="model-preset"><strong>DeepSeek</strong><code>https://api.deepseek.com</code><button type="button" className="text-btn" onClick={()=>{setForm(f=>({...f,protocol:'openai',base_url:'https://api.deepseek.com',model:'',api_key:'',enabled:false}));setDirty(true);setResult('')}}>填入地址</button></div><div className="model-preset"><strong>千问 / 百炼</strong><small>从百炼控制台复制对应业务空间、地域的 OpenAI 兼容地址。</small></div></section><section className="panel"><ShieldCheck size={24}/><h2>连接后的回答边界</h2><p>模型只获得本企业已发布的相关知识和必要对话。价格与效果候选不进入回答，人工接管后停止 AI 自主回复。</p><p>模型超时、引用无效或引入未核实数字时，回退到知识库答案。提示词约束与基础校验仍需配合业务评测。</p><p className="footnote">测试会发起一次最小模型请求，可能产生少量服务商费用。未填写密钥不会调用外部模型。当前仍为本机 Demo，身份免密切换尚需升级为正式登录。</p></section></aside>
    </div></div>
}
