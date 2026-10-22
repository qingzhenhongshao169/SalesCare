export function followMessages(options: {
  url:string; token:()=>string; cursor?:number; signal:AbortSignal;
  onMessages:(data:{items:Record<string,any>[];state:string;case_updates?:Record<string,any>[]})=>void;
  onState?:(connected:boolean)=>void;onExpired?:()=>void;
}):Promise<void>;
