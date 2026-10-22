/** Authenticated, cursor-based SSE with bounded frames and reconnects. */
export async function followMessages({url, token, cursor=0, signal, onMessages, onState=()=>{}, onExpired=()=>{}, fetcher=fetch, pause=abortablePause}) {
  let position=cursor, backoff=200;
  while (!signal.aborted) {
    let reader;
    try {
      const response=await fetcher(`${url}?cursor=${position}`, {headers:{Authorization:`Bearer ${token()}`},signal,cache:'no-store'});
      if ([401,403,404,410].includes(response.status)) { onExpired();return; }
      if (!response.ok || !response.body) throw new Error('stream_unavailable');
      onState(true);reader=response.body.getReader();
      const decoder=new TextDecoder();let buffer='';
      while (!signal.aborted) {
        const {done,value}=await reader.read();
        if (done) break;
        buffer+=decoder.decode(value,{stream:true});
        buffer=buffer.replace(/\r\n/g,'\n');
        if (buffer.length>1_000_000) throw new Error('stream_frame_limit');
        let end;
        while ((end=buffer.indexOf('\n\n'))>=0) {
          const frame=buffer.slice(0,end);buffer=buffer.slice(end+2);
          const lines=frame.split('\n');
          if (!lines.some(line=>line==='event: messages')) continue;
          const raw=lines.filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n');
          const data=JSON.parse(raw);
          if (!Array.isArray(data.items) || typeof data.state!=='string') throw new Error('stream_frame_format');
          const items=data.items.filter(m=>Number.isSafeInteger(m.sequence)&&m.sequence>position&&typeof m.text==='string'&&['visitor','ai','human'].includes(m.role));
          items.sort((a,b)=>a.sequence-b.sequence);
          const unique=items.filter((m,i)=>i===0||m.sequence!==items[i-1].sequence);
          if (unique.length) position=unique.at(-1).sequence;
          if (!signal.aborted) onMessages({...data,items:unique});
          backoff=200;
        }
      }
    } catch (error) {
      if (signal.aborted) return;
    } finally {
      if (reader) {try {await reader.cancel();}catch{}}
      onState(false);
    }
    if (!signal.aborted) {
      await pause(backoff,signal);backoff=Math.min(5000,backoff*2);
    }
  }
}

function abortablePause(ms,signal) {
  return new Promise(resolve=>{
    if (signal.aborted) return resolve();
    const finish=()=>{clearTimeout(timer);signal.removeEventListener('abort',finish);resolve();};
    const timer=setTimeout(finish,ms);signal.addEventListener('abort',finish,{once:true});
  });
}
