(function(root){
  'use strict';
  function init({document,fetch,token,confirm,onChanged}){
    const input=document.getElementById('config-root-path'),status=document.getElementById('config-root-status');
    const preview=document.getElementById('btn-config-preview'),apply=document.getElementById('btn-config-apply'),reset=document.getElementById('btn-config-auto');
    if(!input || !status || !preview || !apply || !reset) return null;
    let pending=null,revision=0,busy=false,writing=false;
    const controls=()=>{
      input.disabled=preview.disabled=reset.disabled=busy||writing;
      apply.disabled=busy||writing||!pending||pending.text!==input.value;
    };
    const request=async(path,body)=>{
      const options={headers:{'X-SpeedBench-Token':token}};
      if(body!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
      const response=await fetch(path,options);return response.json();
    };
    request('/api/config-root').then(r=>{
      if(revision) return;
      if(!r.ok) throw Error('Unavailable');
      input.value=r.path||'';
      status.textContent=r.mode==='invalid'?'启动环境中的目录设置无效，未回退；请重新选择或显式恢复自动发现。':
        r.mode==='custom'?`本次后端使用自定义目录：${r.path}。这不表示已连接或来源已核验。`:'当前使用自动发现。未选择自定义目录。';
    }).catch(()=>{if(!revision)status.textContent='配置目录状态暂不可用；没有修改设置。';});
    input.addEventListener('input',()=>{revision++;pending=null;status.textContent='输入已改变；请先预览，不要粘贴订阅内容或密钥。';controls();});
    preview.addEventListener('click',async()=>{
      if(busy||writing) return;
      const text=input.value,stamp=++revision;pending=null;controls();
      status.textContent='正在核验固定文件布局；不会读取任意文件或修改 Verge…';
      try{
        const r=await request('/api/config-root/preview',{root:text});
        if(revision!==stamp||input.value!==text) return;
        if(!r.ok){status.textContent='目录无效；请检查本机绝对路径及 clash-verge.yaml、profiles.yaml、profiles 布局。原设置未变。';return;}
        pending={text,path:r.path};
        status.textContent=r.path?`布局可用：${r.path}。尚未应用，未核验连接／订阅来源。`:'将恢复自动发现；尚未应用。';
      }catch(e){if(revision===stamp)status.textContent='预览失败；没有应用目录，请检查本机后端连接。';}
      finally{controls();}
    });
    async function save(candidate){
      if(busy||writing||candidate!==pending||candidate.text!==input.value) return;
      writing=true;revision++;controls();
      try{
        const r=await request('/api/config-root',{root:candidate.text});
        if(!r.ok){status.textContent='目录未应用；任务／清理可能仍在进行，或布局已改变。请刷新后重试。';return;}
        pending=null;input.value=r.path||'';
        status.textContent=r.path?'自定义目录已应用于本次后端，正在刷新来源。历史／收藏没有删除。':'已恢复自动发现，正在刷新来源。';
        try{await onChanged();status.textContent+=' 来源已刷新；连接与映射仍以目录状态为准。';}
        catch(e){status.textContent+=' 来源刷新失败，请手动刷新；目录设置已应用。';}
      }catch(e){status.textContent='无法确认目录是否应用；请刷新页面核对，不会自动重发写操作。';}
      finally{writing=false;controls();}
    }
    apply.addEventListener('click',()=>{
      if(!pending||busy||writing||pending.text!==input.value) return;
      const candidate=pending;
      confirm('确认使用此配置目录？仅影响本次后端及后续任务，不改 Verge 或历史；将清除当前待测选择并重新核验来源。',()=>save(candidate));
    });
    reset.addEventListener('click',()=>{
      if(busy||writing) return;
      revision++;input.value='';pending={text:'',path:null};controls();const candidate=pending;
      confirm('确认恢复自动发现？仅影响本次后端，后续任务将重新发现本机配置。',()=>save(candidate));
    });
    controls();
    return Object.freeze({setBusy(value){busy=!!value;controls();}});
  }
  const api=Object.freeze({init});
  if(typeof module==='object'&&module.exports)module.exports=api;else root.SBConfigRoot=api;
})(typeof window==='object'?window:globalThis);
