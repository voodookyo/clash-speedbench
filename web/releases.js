(function(root){
  'use strict';
  const URL='https://github.com/voodookyo/clash-speedbench/releases';
  const errors={timeout:'查询超时',rate_limited:'GitHub 限流或拒绝访问',not_published:'未找到正式 Release',
    invalid_response:'官方响应格式或版本不受支持',unavailable:'官方服务暂不可用'};
  function text(value){
    const r=value&&typeof value==='object'?value:{};
    const version=typeof r.current==='string'&&/^v?\d+\.\d+\.\d+(?:-[\w.-]+)?$/.test(r.current)?r.current:'未知';
    const current=`当前 ${version}${r.current_prerelease?'（预发布版）':''}`;
    if(r.status==='not_checked') return `${current}；尚未联网检查。`;
    if(r.status!=='ok') return `${current}；${errors[r.status]||'检查未完成'}，无法确认是否需要升级。可手动查看官方 Release。`;
    const tag=typeof r.latest==='string'&&/^v?\d+\.\d+\.\d+$/.test(r.latest)?r.latest:'未知';
    const comparison={update_available:'有较新的正式版本，请先核对平台与发行说明',
      current:'与官方最新正式版本号一致；这不代表包签名或完整性已验证',
      ahead:'当前版本号高于正式 Release；不自动降级，也未检查预发布更新'}[r.comparison]||'无法比较本地版本';
    return `${current}；官方正式 Release ${tag}。${comparison}。${r.cached?'（内存缓存结果）':''}`;
  }
  const api=Object.freeze({URL,text});
  if(typeof module==='object'&&module.exports) module.exports=api;
  else root.SBReleases=api;
})(typeof window==='object'?window:globalThis);
