(function(root){
  'use strict';
  const modes={legacy:'兼容模式',quick:'快速',standard:'标准',deep:'深度',ip:'IP 专项'};
  const profiles={balanced:'综合',daily:'日常',download:'下载',ip:'IP 质量',residential:'住宅优先'};
  const statuses={completed:'已完成',cancelled:'已取消',failed:'失败',interrupted:'应用中断',
    queued:'排队中',preparing:'准备中',probing:'探测中',measuring:'精测中',enriching:'画像查询中',
    finalizing:'保存与清理中',cancelling:'正在取消与清理'};
  const labels={completed:'完成',failed:'失败',not_requested:'未请求',not_selected:'未选择',
    cancelled:'已取消',interrupted:'中断',partial:'部分',pending:'等待',unknown:'未知'};
  const own=(map,key)=>typeof key==='string' && Object.prototype.hasOwnProperty.call(map,key)?map[key]:null;
  const number=value=>typeof value==='number' && Number.isFinite(value) && value>=0;
  function describe(value){
    const rec=value&&typeof value==='object'?value:{},t=rec.task&&typeof rec.task==='object'?rec.task:{};
    const rows=Array.isArray(rec.results)?rec.results:[];
    const selected=Number.isSafeInteger(t.selected_node_count) && t.selected_node_count>=rows.length && t.selected_node_count<=3000;
    const status=own(statuses,t.status)||'状态未知';
    const partial=typeof t.partial==='boolean'?(t.partial?'部分结果':'完整结果'):'结果完整性未知';
    const coverage=[['probe','探测'],['bandwidth','带宽'],['intel','IP 画像']].map(([key,name])=>{
      const counts={};
      for(const row of rows){
        const state=row?.measurement_scope?.[key];
        const normalized=own(labels,state)?state:'unknown';counts[normalized]=(counts[normalized]||0)+1;
      }
      const rest=Object.keys(labels).filter(k=>k!=='completed' && counts[k]).map(k=>`${labels[k]} ${counts[k]}`);
      return `${name} ${counts.completed||0}/${rows.length} 完成${rest.length?'（'+rest.join('、')+'）':''}`;
    }).join('；');
    return {mode:`${own(modes,t.mode)||'模式未知'} · ${own(profiles,t.target_profile)||'目标未知'}`,
      range:`已返回 ${rows.length}${selected?' / '+t.selected_node_count:''} 节点${selected?'':'（原范围未知）'}`,
      status:status+' · '+partial,elapsed:number(t.elapsed_ms)?(t.elapsed_ms/1000).toFixed(2)+'s':'未知',
      traffic:Number.isSafeInteger(t.downloaded_bytes) && t.downloaded_bytes>=0?(t.downloaded_bytes/1000000).toFixed(2)+' MB（仅已报告实际字节）':'未知',coverage};
  }
  const api=Object.freeze({describe});
  if(typeof module==='object'&&module.exports) module.exports=api;
  else root.SBHistory=api;
})(typeof window==='object'?window:globalThis);
