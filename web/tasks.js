/* Task protocol/transport and non-sensitive preferences; no DOM or API keys. */
(function(root){
  'use strict';
  const terminal = status => ['completed','cancelled','failed','interrupted'].includes(status);
  const rowKey = row => row.node_id || `legacy:${row.proto||''}|${row.name||''}`;
  class TaskState {
    constructor(snapshot){ this.replace(snapshot); }
    replace(snapshot){
      this.value = JSON.parse(JSON.stringify(snapshot||{}));
      this.rows = new Map((this.value.results||[]).map(r=>[rowKey(r),r]));
      this.value.results = [...this.rows.values()];
    }
    apply(event){
      const v = this.value;
      if(event.version!==1 || event.job_id!==v.job_id || !Number.isSafeInteger(event.seq) || event.seq<=v.seq) return 'ignored';
      if(event.seq!==v.seq+1) return 'resync';
      v.seq = event.seq;
      const p = event.payload || {};
      if(p.status) v.status = p.status;
      if(event.type==='phase_started'){ v.status=event.phase||p.status; v.progress=null; }
      if(event.type.startsWith('node_') && p.result){
        const key = rowKey(p.result);
        this.rows.set(key,Object.assign({},this.rows.get(key)||{},p.result));
        v.results=[...this.rows.values()];
      }
      if(p.total!==undefined) v.progress={kind:event.type,completed:p.completed,total:p.total};
      if(p.metrics){
        v.metrics=v.metrics||{};
        for(const [phase,metric] of Object.entries(p.metrics)){
          const target=v.metrics[phase]||(v.metrics[phase]={duration_ms:0,attempts:0,successes:0,bytes:0});
          for(const key of ['duration_ms','attempts','successes','bytes']) target[key]+=metric[key]||0;
        }
      }
      if(terminal(v.status)){ v.partial=v.status!=='completed'; v.finished_at=event.timestamp; }
      return 'updated';
    }
  }
  class Client {
    constructor(options){
      this.options=options; this.generation=0; this.source=null; this.timer=null; this.state=null;
      this.now=options.now||(()=>performance.now());this.elapsed=null;this.received=0;
    }
    close(){
      this.generation++;
      if(this.source) this.source.close();
      if(this.timer) clearTimeout(this.timer);
      this.source=null; this.timer=null;
    }
    receivedSnapshot(snapshot){this.elapsed=Number.isFinite(snapshot.elapsed_ms)?snapshot.elapsed_ms:null;this.received=this.now();}
    notify(){
      if(this.elapsed!==null && !terminal(this.state.value.status)) this.state.value.elapsed_ms=this.elapsed+Math.max(0,this.now()-this.received);
      this.options.onChange(this.state.value);
    }
    async attach(jobId){
      this.close();
      const generation=this.generation;
      const snapshot=await this.options.get('/api/jobs/'+jobId);
      if(generation!==this.generation) return;
      if(snapshot.version!==1 || snapshot.job_id!==jobId) throw new Error('任务已失效，请刷新任务中心');
      this.state=new TaskState(snapshot); this.receivedSnapshot(snapshot);this.notify();
      if(!terminal(snapshot.status)) this.connect(generation);
    }
    async refresh(generation){
      try{
        const snapshot=await this.options.get('/api/jobs/'+this.state.value.job_id);
        if(generation!==this.generation) return;
        if(snapshot.job_id!==this.state.value.job_id || snapshot.version!==1) throw new Error('任务状态不可用');
        if(snapshot.seq>=this.state.value.seq){ this.state.replace(snapshot);this.receivedSnapshot(snapshot);this.notify(); }
        if(terminal(this.state.value.status)) this.close();
      }catch(error){ if(generation===this.generation && this.options.onError) this.options.onError(error); }
    }
    connect(generation){
      const EventSource=this.options.EventSource;
      if(!EventSource){
        this.timer=setTimeout(async()=>{
          await this.refresh(generation);
          if(generation===this.generation && !terminal(this.state.value.status)) this.connect(generation);
        },2000);
        return;
      }
      const source=new EventSource('/api/jobs/'+this.state.value.job_id+'/stream?since_seq='+this.state.value.seq);
      this.source=source;
      source.addEventListener('progress',event=>{
        if(generation!==this.generation) return;
        try{
          const changed=this.state.apply(JSON.parse(event.data));
          if(changed==='resync'){ this.refresh(generation); return; }
          if(changed==='updated') this.notify();
          if(terminal(this.state.value.status)) this.refresh(generation);
        }catch(error){ this.refresh(generation); }
      });
      source.addEventListener('snapshot',event=>{
        if(generation!==this.generation) return;
        try{
          const snapshot=JSON.parse(event.data);
          if(snapshot.job_id===this.state.value.job_id && snapshot.seq>=this.state.value.seq){
            this.state.replace(snapshot);this.receivedSnapshot(snapshot);this.notify();
            if(terminal(snapshot.status)) this.close();
          }
        }catch(error){ this.refresh(generation); }
      });
      // The server deliberately bounds each stream. EventSource reconnects
      // with Last-Event-ID; a snapshot check also recovers a missed terminal.
      source.addEventListener('error',()=>{ if(generation===this.generation) this.refresh(generation); });
    }
  }
  function migrateFavorites(names,nodes,existing=[]){
    const ids=new Set(existing), pending=[];
    for(const name of names){
      const matches=(nodes||[]).filter(n=>n.runtime_name===name && n.identity_strength==='strong' && n.node_id);
      const unique=[...new Set(matches.map(n=>n.node_id))];
      if(unique.length===1) ids.add(unique[0]); else pending.push(name);
    }
    return {ids:[...ids],pending};
  }
  function budget(config,count){
    if(!config || !config.bandwidth) return 0;
    const nodes=config.measure_all?count:Math.min(count,config.top_n);
    return nodes*((config.mb||95)*config.rounds*(config.multi?5:1)+(config.mb==null?1:0));
  }
  const api={terminal,rowKey,TaskState,Client,migrateFavorites,budget};
  if(typeof module!=='undefined' && module.exports) module.exports=api;
  root.SBTasks=api;
})(typeof globalThis!=='undefined'?globalThis:this);
