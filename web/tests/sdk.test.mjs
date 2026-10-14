import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const source=readFileSync(new URL('../../mvp/sdk.js',import.meta.url),'utf8');
function setup(dataset={}){
 const nodes=[],handlers=new Map(),events=[];
 const window={addEventListener:(k,fn)=>handlers.set(k,fn),removeEventListener:(k)=>handlers.delete(k),dispatchEvent:e=>events.push(e)};
 const document={currentScript:{src:'https://service.example/sdk/v1/salescare.js',dataset},body:{append:(...items)=>nodes.push(...items)},createElement:tag=>({tag,style:{},attributes:{},contentWindow:{},setAttribute(k,v){this.attributes[k]=v},addEventListener(k,fn){this[k]=fn},remove(){this.removed=true}})};
 const context=vm.createContext({window,document,URL,URLSearchParams,location:{origin:'https://merchant.example',href:'https://merchant.example/'},Map,Set,setTimeout:()=>1,clearTimeout:()=>{},CustomEvent:class {constructor(type,options){this.type=type;this.detail=options.detail}}});
 vm.runInContext(source,context);return {window,document,nodes,handlers,events,context};
}
const options={company:'test-company',channel:'a'.repeat(32)};
test('SDK deduplicates script installation and cleans up all controls',()=>{
 const s=setup();const instance=s.window.SalesCare.init(options);assert.equal(s.nodes.length,2);assert.equal(s.window.SalesCare.init(options),instance);instance.open();assert.equal(s.nodes[1].hidden,false);instance.close();assert.equal(s.nodes[1].hidden,true);instance.destroy();assert.ok(s.nodes.every(n=>n.removed));assert.equal(s.handlers.size,0);s.window.SalesCare.init(options);assert.equal(s.nodes.length,4);
});
test('SDK accepts only its exact iframe and service origin',()=>{
 const s=setup();s.window.SalesCare.init(options);const receive=s.handlers.get('message'),frame=s.nodes[1];
 receive({origin:'https://evil.example',source:frame.contentWindow,data:{type:'salescare.ready',config:{button_text:'wrong'}}});assert.equal(s.events.length,0);
 receive({origin:'https://service.example',source:{},data:{type:'salescare.ready'}});assert.equal(s.events.length,0);
 receive({origin:'https://service.example',source:frame.contentWindow,data:{type:'salescare.ready',config:{button_text:'咨询品牌',position:'left',color:'#123456'}}});assert.equal(s.nodes[0].textContent,'咨询品牌');assert.equal(s.nodes[0].style.left,'24px');assert.equal(s.events[0].type,'salescare:ready');
});
test('SDK validates public identifiers and never includes credentials in iframe URL',()=>{
 const s=setup();assert.throws(()=>s.window.SalesCare.init({...options,company:'../wrong'}));assert.throws(()=>s.window.SalesCare.init({...options,channel:'bad'}));s.window.SalesCare.init({...options,apiKey:'must-not-leak',secret:'must-not-leak'});assert.ok(!s.nodes[1].src.includes('must-not-leak'));assert.match(s.nodes[1].src,/parent_origin=/);
});
test('SDK supports auto install and independent channel instances',()=>{
 const s=setup(options);assert.equal(s.nodes.length,2);s.window.SalesCare.init({...options,channel:'b'.repeat(32)});assert.equal(s.nodes.length,4);assert.notEqual(s.nodes[1].src,s.nodes[3].src);
});
test('SDK events contain bounded state metadata and isolate failing callbacks',()=>{
 const s=setup();const a=s.window.SalesCare.init(options);let calls=0;a.on('state',()=>{throw new Error('subscriber')});a.on('state',()=>calls++);s.handlers.get('message')({origin:'https://service.example',source:s.nodes[1].contentWindow,data:{type:'salescare.state',state:'HUMAN_ACTIVE',text:'private'}});assert.equal(calls,1);assert.equal(s.events[0].detail.text,undefined);
});
