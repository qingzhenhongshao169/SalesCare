/** Synthetic public-browser checks on the original SalesCare service/provider. */
const fs=require('node:fs'),path=require('node:path');
const {createRequire}=require('node:module');
const {performance}=require('node:perf_hooks');
const {chromium}=createRequire(path.join(process.env.SALESCARE_QA_NODE_MODULES,'package.json'))('playwright');
const root=path.resolve(__dirname,'..');
const run=process.argv[2]==='final'?'final-':'';
let browser;
(async()=>{
 browser=await chromium.launch({headless:true,executablePath:process.env.SALESCARE_QA_BROWSER});
 const page=await browser.newPage({viewport:{width:1366,height:960}});
 const report={date:'2026-10-20',scope:'Original Biaofun service and configured provider; synthetic internal conversations, no capacity or two-second guarantee.',rows:[],conversations:[],page_errors:[]};
 page.on('pageerror',e=>report.page_errors.push(e.message));
 await page.goto('http://127.0.0.1:8767/chat/biaofun');
 const input=page.getByRole('textbox',{name:'咨询内容'});await input.waitFor();let cid;
 function check(ok,label){if(!ok)throw new Error(label);}
 async function fresh(label){
  const pending=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/public/conversations'&&r.request().method()==='POST');
  await page.getByRole('button',{name:'新咨询',exact:true}).click();cid=(await (await pending).json()).id;
  report.conversations.push({id:cid,label});
  await page.waitForFunction(()=>document.querySelectorAll('.message.ai').length===1&&document.querySelectorAll('.message.visitor').length===0);
 }
 async function send(text,label){
  const count=await page.locator('.message.ai').count();await input.fill(text);const start=performance.now();await input.press('Enter');
  await page.waitForFunction(count=>document.querySelectorAll('.message.ai').length>count,count,{timeout:45000});
  const elapsed_ms=Math.round(performance.now()-start);
  const detail=await page.evaluate(async id=>(await fetch('/api/public/conversations/'+id,{credentials:'same-origin'})).json(),cid);
  const row={label,conversation_id:cid,question:text,answer:detail.messages.at(-1).text,profile:detail.profile,case_count:detail.messages.at(-1).case_recommendations.length,elapsed_ms};
  report.rows.push(row);console.log(JSON.stringify({label,elapsed_ms,cases:row.case_count}));return row;
 }
 async function snapshot(name){
  await page.locator('.messages').evaluate(el=>el.scrollTop=el.scrollHeight);
  await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));
  await page.screenshot({path:path.join(root,'docs/p1-'+run+name+'-20261020.png'),fullPage:true});
 }
 try{
  await fresh('choice-and-new-project');
  await send('先做管理系统，预算8万，下个月上线','system');
  let r=await send('你们做过官网吗？','case-question');
  check(r.profile.need.value==='管理系统'&&r.profile.budget.value===80000,'Case question changed the selected project');
  await snapshot('choice');
  r=await send('换个项目，重新开始','restart');
  check(!r.profile.need&&!r.profile.budget&&!r.profile.timeline,'Restart retained old requirements');
  check(!/8万|80000|下个月/.test(r.answer),'Reply retained old project requirements');
  await snapshot('restart');
  r=await send('先做小程序，预算3万元','new-project');
  check(r.profile.need.value==='小程序'&&r.profile.budget.value===30000&&!r.profile.timeline,'New project inherited an old deadline');
  await fresh('cancellation-and-budget');
  await send('先做官网，预算8w','budget8w');
  r=await send('官网不做了，先做小程序','replace');check(r.profile.need.value==='小程序','Cancelled direction survived');
  r=await send('暂时不要做小程序了','cancel');check(!r.profile.need,'Explicit cancellation ignored');
  check(/暂停|不做|不再|取消|搁置|放下/.test(r.answer),'Cancellation reply kept promoting the old service');
  r=await send('现在变9万了','budget9w');check(r.profile.budget.value===90000&&r.case_count===0,'Budget regression');
  await fresh('compound');
  r=await send('我们做机械出口，想建英文官网。请说说要准备什么资料，也给个相关案例；预算还没定。','compound');
  check(r.case_count===1&&r.profile.budget_status?.value==='unset','Compound evidence regression');
  check(/公司介绍|企业介绍/.test(r.answer)&&/产品/.test(r.answer),'Materials task lost');
  await fresh('materials');
  r=await send('我想做企业官网，需要准备哪些资料？','materials');
  check(/公司介绍|企业介绍/.test(r.answer)&&/产品/.test(r.answer),'Website material answer regressed');
  await page.reload();await input.waitFor();check(report.page_errors.length===0,'Page script error');
  report.passed=true;
 }finally{
  fs.writeFileSync(path.join(root,'docs/p1-browser-'+run+'verification-20261020.json'),JSON.stringify(report,null,2));
  await browser.close();
 }
})().catch(async e=>{console.error(e.message);if(browser)await browser.close().catch(()=>{});process.exitCode=1;});
