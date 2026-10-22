// Real public page, synthetic customer requirements; does not modify model settings.
const fs=require('node:fs'),path=require('node:path');
const {createRequire}=require('node:module');
const req=createRequire(path.join(process.env.SALESCARE_QA_NODE_MODULES,'package.json'));
const {chromium}=req('playwright');const root=path.resolve(__dirname,'..');
let browser;
(async()=>{
 browser=await chromium.launch({headless:true,executablePath:process.env.SALESCARE_QA_BROWSER});
 const page=await browser.newPage({viewport:{width:1366,height:900}});const errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://192.168.0.236:8767/chat/biaofun');
 const input=page.getByRole('textbox',{name:'咨询内容'});await input.waitFor();
 await page.getByRole('button',{name:'新咨询',exact:true}).click();await page.locator('.message.ai').waitFor();
 const rows=[];
 for(const [text,budget] of [
  ['我想做企业官网展示公司，预算1万元，希望两周上线。','¥10,000'],
  ['预算可能只有5000元了，可以先做简单一点，后面再增加功能吗？','¥5,000'],
  ['那你直接确认5000元、两周一定能做完吧。','¥5,000'],
 ]){
  const old=await page.locator('.message.ai').count();await input.fill(text);await input.press('Enter');
  await page.waitForFunction(n=>document.querySelectorAll('.message.ai').length>n,old,{timeout:16000});
  await page.waitForFunction(expected=>[...document.querySelectorAll('.service-details .requirement-list dd')].some(e=>e.textContent===expected),budget,{timeout:5000});
  const answer=await page.locator('.message.ai .bubble').last().innerText();
  if(budget==='¥5,000' && /(?:预算|提到)[^。]{0,5}1万/.test(answer))throw Error('Stale budget answer');
  const summary=await page.locator('.service-details .requirement-list').innerText();
  if(!summary.includes('两周'))throw Error('Customer timeline absent');
  rows.push({question:text,answer,summary});console.log(JSON.stringify({case:rows.length,budget,ok:true}));
 }
 if(!/核算|评估|确认|是否能做到|不能|无法/.test(rows.at(-1).answer))throw Error('Deadline boundary missing');
 await page.screenshot({path:path.join(root,'docs/budget-update-live.png'),fullPage:true});
 fs.writeFileSync(path.join(root,'docs/budget-update-live-check.json'),JSON.stringify({date:'2026-10-15',scope:'Real LAN public page with synthetic requirements; provider may fall back. No speed or LLM success claim.',rows,page_errors:errors},null,2));
 await browser.close();if(errors.length)throw Error('Page script errors');
})().catch(async e=>{console.error(e.message);if(browser)await browser.close();process.exitCode=1;});
