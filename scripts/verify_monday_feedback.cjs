/** Synthetic browser regression on the existing Biaofun service and provider. */
const fs=require('node:fs'),path=require('node:path');
const {createRequire}=require('node:module');
const {performance}=require('node:perf_hooks');
const req=createRequire(path.join(process.env.SALESCARE_QA_NODE_MODULES,'package.json'));
const {chromium}=req('playwright');
const root=path.resolve(__dirname,'..');
const phase=process.argv[2]||'after';if(!['before','after'].includes(phase))throw Error('before or after required');
let browser;
(async()=>{
 browser=await chromium.launch({headless:true,executablePath:process.env.SALESCARE_QA_BROWSER});
 const page=await browser.newPage({viewport:{width:1366,height:960}});
 const rows=[],conversations=[],errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://192.168.0.236:8767/chat/biaofun');
 const input=page.getByRole('textbox',{name:'咨询内容'});await input.waitFor();
 async function fresh(label){
  const created=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/public/conversations'&&r.request().method()==='POST');
  await page.getByRole('button',{name:'新咨询',exact:true}).click();
  const cid=(await (await created).json()).id;conversations.push({label,id:cid});
  await page.waitForFunction(()=>document.querySelectorAll('.message.ai').length===1&&document.querySelectorAll('.message.visitor').length===0);
 }
 async function send(question,label){
  const count=await page.locator('.message.ai').count();await input.fill(question);const at=performance.now();await input.press('Enter');
  await page.waitForFunction(({count,question})=>{
   const ai=[...document.querySelectorAll('.message.ai')];const user=[...document.querySelectorAll('.message.visitor')].at(-1);
   return ai.length>count&&user?.querySelector('.bubble')?.textContent===question&&
    Boolean(user.compareDocumentPosition(ai.at(-1))&Node.DOCUMENT_POSITION_FOLLOWING);
  },{count,question},{timeout:45000});
  const last=page.locator('.message.ai').last();
  const row={label,conversation_id:conversations.at(-1).id,question,answer:await last.locator('.bubble').innerText(),
   case_count:await last.locator('.case-card').count(),elapsed_ms:Math.round(performance.now()-at),summary:await page.locator('.service-details .requirement-list').innerText()};
  rows.push(row);console.log(JSON.stringify({phase,label,cases:row.case_count,elapsed_ms:row.elapsed_ms}));return row;
 }
 function check(value,message){if(phase==='after'&&!value)throw Error(message);}
 async function snapshot(label){
  if(phase!=='after')return;
  await page.locator('.messages').evaluate(el=>{el.style.scrollBehavior='auto';el.scrollTop=el.scrollHeight;});
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  await page.screenshot({path:path.join(root,'docs/monday-feedback-'+label+'-20261019.png'),fullPage:true});
 }
 await fresh('compound');
 let row=await send('我们做机械出口，想建英文官网。请说说要准备什么资料，也给个相关案例；预算还没定。','compound');
 check(row.case_count===1,'Compound question lost approved case');
 check(/公司介绍|企业介绍/.test(row.answer)&&/产品/.test(row.answer),'Compound question lost materials');
 check(!/没有.{0,12}案例|不能提供.{0,6}案例/.test(row.answer),'False no-case claim');
 if(phase==='after')await page.waitForFunction(()=>[...document.querySelectorAll('.requirement-list dd')].some(e=>e.textContent==='未定'));
 await snapshot('compound');
 await fresh('undecided');
 await send('我们想做数字化升级','digital_goal');
 row=await send('具体做网站还是系统还没定','undecided');
 check(row.case_count===0,'Undecided direction promoted a case');
 row=await send('你先给个建议。','advice');
 check(/官网|网站/.test(row.answer)&&/系统|内部流程/.test(row.answer),'Comparison lost a direction');
 check(!/先从官网|先做官网|官网先承担/.test(row.answer),'Premature website selection');
 if(phase==='after')await page.waitForFunction(()=>[...document.querySelectorAll('.requirement-list dd')].some(e=>e.textContent?.includes('未定')&&e.textContent?.includes('系统')));
 await snapshot('undecided');
 row=await send('那就先做官网吧，业务系统以后再考虑','choose');
 if(phase==='after')await page.waitForFunction(()=>[...document.querySelectorAll('.requirement-list dd')].some(e=>e.textContent==='企业官网'));
 await fresh('dispatch');
 row=await send('你们给物流公司做过运输调度系统吗？我不要展示型官网','dispatch');
 check(row.case_count===0&&!/佳怡.{0,15}调度系统/.test(row.answer),'Website case became dispatch software experience');
 await fresh('selection');
 await send('工业设备官网的产品参数很多，怎样让客户快速找到适合的型号？','selection');
 await fresh('budget');
 await send('工业企业想做官网，预算8w','budget_8w');
 if(phase==='after')await page.waitForFunction(()=>[...document.querySelectorAll('.requirement-list dd')].some(e=>e.textContent==='¥80,000'));
 row=await send('现在变9万了','budget_change');
 check(row.case_count===0,'Budget correction promoted a case');
 if(phase==='after')await page.waitForFunction(()=>[...document.querySelectorAll('.requirement-list dd')].some(e=>e.textContent==='¥90,000'));
 await page.reload();await input.waitFor();
 if(phase==='after')await page.waitForFunction(()=>[...document.querySelectorAll('.requirement-list dd')].some(e=>e.textContent==='¥90,000'));
 const report={phase,date:'2026-10-19',scope:'Original Biaofun service, real configured provider, synthetic internal consultations. Sequential convenience samples, no P95 or causal latency claim.',rows,conversations,page_errors:errors};
 fs.writeFileSync(path.join(root,'docs/monday-feedback-browser-'+phase+'-20261019.json'),JSON.stringify(report,null,2));
 await browser.close();if(errors.length)throw Error('Browser script errors');
})().catch(async e=>{console.error(e.message);if(browser)await browser.close();process.exitCode=1;});
