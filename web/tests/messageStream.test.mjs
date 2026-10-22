import test from 'node:test';
import assert from 'node:assert/strict';
import {followMessages} from '../mvp/messageStream.mjs';
const encoder=new TextEncoder();
const frame=(sequence,text='你好')=>`id: ${sequence}\nevent: messages\ndata: ${JSON.stringify({items:[{sequence,text,role:'ai',id:'m'+sequence}],state:'AI_ACTIVE'})}\n\n`;
const response=chunks=>new Response(new ReadableStream({start(c){for(const chunk of chunks)c.enqueue(chunk);c.close();}}));

test('SSE reconnect resumes from cursor, deduplicates and handles split UTF8', async()=>{
  const abort=new AbortController(),urls=[],messages=[];let calls=0;
  const bytes=encoder.encode(frame(1));
  await followMessages({url:'/events',token:()=>'synthetic-token',signal:abort.signal,
    fetcher:async(url,options)=>{urls.push(url);assert.equal(options.headers.Authorization,'Bearer synthetic-token');return ++calls===1?response([bytes.slice(0,38),bytes.slice(38,43),bytes.slice(43)]):response([encoder.encode(frame(1)+frame(2,'第二条'))]);},
    onMessages:data=>{messages.push(...data.items);if(messages.length===2)abort.abort();},pause:async()=>{}});
  assert.deepEqual(urls,['/events?cursor=0','/events?cursor=1']);
  assert.deepEqual(messages.map(m=>m.text),['你好','第二条']);
});

test('SSE does not retry revoked credentials or emit after cancellation', async()=>{
  const abort=new AbortController();let expired=0,calls=0;
  await followMessages({url:'/events',token:()=>'',signal:abort.signal,fetcher:async()=>{calls++;return new Response('',{status:403});},onMessages:()=>assert.fail(),onExpired:()=>expired++});
  assert.equal(expired,1);assert.equal(calls,1);
  abort.abort();await followMessages({url:'/events',token:()=>'',signal:abort.signal,fetcher:()=>assert.fail(),onMessages:()=>assert.fail()});
});

test('SSE malformed frames reconnect without moving cursor', async()=>{
  const abort=new AbortController(),urls=[];let calls=0;
  await followMessages({url:'/events',token:()=>'',cursor:7,signal:abort.signal,
    fetcher:async url=>{urls.push(url);return ++calls===1?response([encoder.encode('event: messages\ndata: invalid\n\n')]):response([encoder.encode(frame(8))]);},
    onMessages:data=>{assert.equal(data.items[0].sequence,8);abort.abort();},pause:async()=>{}});
  assert.deepEqual(urls,['/events?cursor=7','/events?cursor=7']);
});
