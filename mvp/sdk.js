/* SalesCare JS SDK v1: public identifiers only; credentials stay on the server. */
(()=>{'use strict';
const script=document.currentScript;
const origin=new URL(script.src,location.href).origin;
const instances=new Map();
function init(options={}){
 const company=String(options.company||''),channel=String(options.channel||''),key=company+':'+channel;
 if(!/^[a-z0-9-]{1,80}$/.test(company)||! /^[a-f0-9]{32}$/.test(channel))throw new Error('SalesCare: invalid public channel identifier');
 if(instances.has(key))return instances.get(key);
 const listeners=new Map();let opened=false,destroyed=false,ready=false;
 const button=document.createElement('button');button.type='button';button.textContent=options.buttonText||'咨询我们';button.setAttribute('aria-label','打开智能客服');button.setAttribute('aria-expanded','false');
 Object.assign(button.style,{position:'fixed',right:'24px',bottom:'24px',background:'#167461',color:'#fff',border:0,borderRadius:'28px',padding:'14px 22px',cursor:'pointer',zIndex:2147483000});
 const frame=document.createElement('iframe');frame.title='企业智能客服';frame.hidden=true;frame.referrerPolicy='strict-origin-when-cross-origin';frame.setAttribute('allow','');
 frame.src=origin+'/chat/'+company+'?'+new URLSearchParams({widget:'1',channel,parent_origin:location.origin});
 Object.assign(frame.style,{position:'fixed',right:'20px',bottom:'84px',width:'min(420px,calc(100vw - 32px))',height:'min(700px,calc(100vh - 110px))',border:'1px solid #dbe5e2',borderRadius:'18px',boxShadow:'0 12px 40px #243c3820',zIndex:2147483000,background:'#fff'});
 function emit(event,data={}){const detail={company,channel,...data};for(const callback of listeners.get(event)||[])try{callback(detail)}catch{}window.dispatchEvent(new CustomEvent('salescare:'+event,{detail}));}
 function setOpen(value){if(destroyed)return;opened=value;frame.hidden=!value;button.setAttribute('aria-expanded',String(value));emit(value?'open':'close');}
 function receive(e){if(e.origin!==origin||e.source!==frame.contentWindow||!e.data||typeof e.data!=='object')return;
  if(e.data.type==='salescare.ready'){ready=true;const cfg=e.data.config||{};if(/^#[a-f0-9]{6}$/i.test(cfg.color||''))button.style.background=cfg.color;if(typeof cfg.button_text==='string'&&cfg.button_text.length<=30)button.textContent=cfg.button_text||'咨询我们';if(cfg.position==='left'){button.style.right='auto';button.style.left='24px';frame.style.right='auto';frame.style.left='20px';}emit('ready');}
  else if(e.data.type==='salescare.close')setOpen(false);
  else if(e.data.type==='salescare.state'&&['AI_ACTIVE','HANDOFF_PENDING','HUMAN_ACTIVE','CLOSED'].includes(e.data.state))emit('state',{state:e.data.state});
  else if(e.data.type==='salescare.error')emit('error',{message:'咨询暂不可用，请稍后重试。'});
 }
 const api={open:()=>setOpen(true),close:()=>setOpen(false),on:(event,callback)=>{if(typeof callback!=='function')throw new Error('SalesCare: callback required');if(!listeners.has(event))listeners.set(event,new Set());listeners.get(event).add(callback);return()=>listeners.get(event)?.delete(callback)},destroy:()=>{if(destroyed)return;destroyed=true;button.remove();frame.remove();window.removeEventListener('message',receive);clearTimeout(timer);instances.delete(key);listeners.clear();}};
 button.addEventListener('click',()=>setOpen(!opened));window.addEventListener('message',receive);
 const mount=()=>{if(!destroyed)document.body.append(button,frame)};
 if(document.body)mount();else document.addEventListener('DOMContentLoaded',mount,{once:true});
 const timer=setTimeout(()=>{if(!ready&&!destroyed)emit('error',{message:'咨询暂不可用，请稍后重试。'})},15000);
 instances.set(key,api);return api;
}
if(!window.SalesCare){window.SalesCare={version:'1.0.0',init};}
if(script.dataset.company&&script.dataset.channel)window.SalesCare.init({company:script.dataset.company,channel:script.dataset.channel});
})();
