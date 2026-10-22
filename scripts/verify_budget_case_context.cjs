/** Real Biaofun browser smoke: synthetic visitor, original KB and provider. No employee credentials. */
const fs=require('node:fs');
const path=require('node:path');
const {createRequire}=require('node:module');
const {performance}=require('node:perf_hooks');
const req=createRequire(path.join(process.env.SALESCARE_QA_NODE_MODULES,'package.json'));
const {chromium}=req('playwright');
const root=path.resolve(__dirname,'..');
let browser;
(async()=>{
 browser=await chromium.launch({headless:true,executablePath:process.env.SALESCARE_QA_BROWSER});
 const page=await browser.newPage({viewport:{width:1366,height:960}});
 const errors=[];const rows=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://192.168.0.236:8767/chat/biaofun');
 const input=page.getByRole('textbox',{name:'咨询内容'});await input.waitFor();
 const created=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/public/conversations'&&r.request().method()==='POST');
 await page.getByRole('button',{name:'新咨询',exact:true}).click();
 const cid=(await (await created).json()).id;
 await page.waitForFunction(()=>document.querySelectorAll('.message.ai').length===1);
 for(const [text,budget,expectedCard] of [
  ['我们是工业设备企业，想做面向海外的官网。',null,false],
  ['预算8w','¥80,000',false],
  ['预算80000，不是8','¥80,000',false],
  ['现在变9万了','¥90,000',false],
  ['是预算变9万','¥90,000',false],
  ['希望展示设备和品牌实力。','¥90,000',false],
  ['官网要准备什么资料？','¥90,000',false],
  ['你们做过相关工业外贸官网吗？请给我看看案例。','¥90,000',true],
 ]){
  const count=await page.locator('.message.ai').count();await input.fill(text);
  const started=performance.now();await input.press('Enter');
  await page.waitForFunction(({old,text})=>{
   const ai=[...document.querySelectorAll('.message.ai')];
   const user=[...document.querySelectorAll('.message.visitor')].at(-1);
   return ai.length>old&&user?.querySelector('.bubble')?.textContent===text&&
    Boolean(user.compareDocumentPosition(ai.at(-1))&Node.DOCUMENT_POSITION_FOLLOWING);
  },{old:count,text},{timeout:45000});
  if(budget)await page.waitForFunction(expected=>[...document.querySelectorAll('.service-details .requirement-list dd')]
   .some(e=>e.textContent===expected),budget,{timeout:5000});
  const last=page.locator('.message.ai').last();
  if(expectedCard)await last.locator('.case-card').waitFor({timeout:5000});
  const cards=await last.locator('.case-card').count();
  if(Boolean(cards)!==expectedCard)throw Error('Unexpected case recommendation on turn '+(rows.length+1));
  const answer=await last.locator('.bubble').innerText();
  if(budget==='¥90,000'&&/(?:预算|提到)[^。]{0,8}(?:80000|80,000|8万|8w)/i.test(answer))throw Error('Stale budget answer');
  rows.push({question:text,answer,budget,case_count:cards,elapsed_ms:Math.round(performance.now()-started)});
  console.log(JSON.stringify({turn:rows.length,budget,case_count:cards,ok:true}));
 }
 await page.locator('.messages').evaluate(el=>{el.style.scrollBehavior='auto';el.scrollTop=el.scrollHeight;});
 await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
 await page.screenshot({path:path.join(root,'docs/budget-case-context-live-20261019.png'),fullPage:true});
 await page.reload();await input.waitFor();
 await page.waitForFunction(()=>[...document.querySelectorAll('.service-details .requirement-list dd')].some(e=>e.textContent==='¥90,000'));
 if(await page.locator('.case-card').count()!==1)throw Error('Refresh changed recommendation count');
 fs.writeFileSync(path.join(root,'docs/budget-case-context-live-20261019.json'),JSON.stringify({date:'2026-10-19',
  scope:'Real original Biaofun browser, original KB and configured provider. Synthetic internal consultation; inspect server outcomes separately, no all-LLM or P95 claim.',
  conversation_id:cid,rows,refresh_budget:'¥90,000',refresh_case_count:1,page_errors:errors},null,2));
 await browser.close();if(errors.length)throw Error('Browser errors');
})().catch(async e=>{console.error(e.message);if(browser)await browser.close();process.exitCode=1;});
