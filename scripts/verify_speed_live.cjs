// Public questions only, real LAN frontend. Exact FAQ/local paths need no model API.
const fs=require('node:fs'),path=require('node:path');
const {createRequire}=require('node:module');
const req=createRequire(path.join(process.env.SALESCARE_QA_NODE_MODULES,'package.json'));
const {chromium}=req('playwright');const root=path.resolve(__dirname,'..');
let browser;
(async()=>{
 browser=await chromium.launch({headless:true,executablePath:process.env.SALESCARE_QA_BROWSER});
 const page=await browser.newPage({viewport:{width:1366,height:900}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const startedAt=new Date().toISOString();
 await page.goto('http://192.168.0.236:8767/chat/biaofun');
 const input=page.getByRole('textbox',{name:'咨询内容'});await input.waitFor();
 await page.getByRole('button',{name:'新咨询',exact:true}).click();await page.locator('.message.ai').waitFor();
 const cases=['你好','你们主要做什么？','标梵互动是什么公司？','如何联系标梵互动？','标梵的服务理念是什么？','标梵可以提供哪些服务？','能同时做网站、开发和营销吗？','高端定制网站和模板网站有什么区别？','企业官网建设包含哪些服务方向？','已有网站能做诊断和改版吗？','网站可以适配电脑和手机吗？','外贸与品牌出海网站能做吗？'];
 const rows=[];
 for(const question of cases){
  const count=await page.locator('.message.ai').count();await input.fill(question);const start=performance.now();await input.press('Enter');
  await page.waitForFunction(n=>document.querySelectorAll('.message.ai').length>n,count,{timeout:13000});
  const row={question,elapsed_ms:Math.round(performance.now()-start),citations:await page.locator('.message.ai').last().locator('.citations a').count()};rows.push(row);console.log(JSON.stringify(row));
 }
 await page.screenshot({path:path.join(root,'docs/response-speed-live.png'),fullPage:true});
 fs.writeFileSync(path.join(root,'docs/response-speed-live-check.json'),JSON.stringify({started_at:startedAt,date:'2026-10-15',scope:'Local Chromium through LAN IP; Biaofun real published standard FAQs. Not a live LLM speed benchmark.',rows,page_errors:errors},null,2));
 await browser.close();
})().catch(async e=>{console.error(e.message);if(browser)await browser.close();process.exitCode=1;});
