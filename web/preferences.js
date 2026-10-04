/* Explicit non-secret transfer. Never enumerate storage or read credential controls. */
(function(root){
  'use strict';
  const FORMAT='clash-speedbench-ui-preferences', MAX_BYTES=262144;
  const ENUMS={sb_theme:['system','light','dark'],sb_profile:['all','daily','download','ipclean','residential'],
    sb_mode:['legacy','quick','standard','deep','ip'],sb_target:['daily','download','balanced','ip','residential'],
    sb_subs_days:['7','30','90','365'],sb_notifications:['off','on']};
  const KEYS=Object.freeze([...Object.keys(ENUMS),'sb_favs','sb_favs_v2']);
  const fail=()=>{throw new Error('偏好格式无效、含不支持字段或超过限制；未应用导入');};
  const plain=v=>v!==null && typeof v==='object' && !Array.isArray(v);
  const bytes=v=>new TextEncoder().encode(v).length;
  function validate(values){
    if(!plain(values)||Object.keys(values).some(k=>!KEYS.includes(k))) fail();
    const out={};
    for(const [key,value] of Object.entries(values)){
      if(typeof value!=='string') fail();
      if(ENUMS[key]){if(!ENUMS[key].includes(value)) fail();out[key]=value;continue;}
      let list;try{list=JSON.parse(value);}catch(e){fail();}
      if(!Array.isArray(list)||list.length>2000||list.some(n=>typeof n!=='string'||[...n].length>512 ||
          [...n].some(c=>c.codePointAt(0)>=0xd800 && c.codePointAt(0)<=0xdfff))) fail();
      if(key==='sb_favs_v2' && list.some(n=>!/^node_v2_[0-9a-f]{32}$/.test(n))) fail();
      out[key]=JSON.stringify([...new Set(list)]);
    }
    if(bytes(JSON.stringify(out))>MAX_BYTES) fail();
    return out;
  }
  function readValues(read){
    const values={};for(const key of KEYS){const value=read(key);if(value!==null && value!==undefined) values[key]=value;}
    return validate(values);
  }
  function exportText(read){
    const text=JSON.stringify({format:FORMAT,version:1,values:readValues(read)},null,2);
    if(bytes(text)>MAX_BYTES) fail();return text;
  }
  function parseImport(text){
    if(typeof text!=='string'||bytes(text)>MAX_BYTES) fail();
    let value;try{value=JSON.parse(text);}catch(e){fail();}
    if(!plain(value)||Object.keys(value).sort().join(',')!=='format,values,version' ||
        value.format!==FORMAT||value.version!==1) fail();
    return validate(value.values);
  }
  function merge(current,incoming){
    current=validate(current);incoming=validate(incoming);
    const result=Object.assign({},current,incoming);
    for(const key of ['sb_favs','sb_favs_v2']){
      if(key in incoming) result[key]=JSON.stringify([...new Set([
        ...JSON.parse(current[key]||'[]'),...JSON.parse(incoming[key])])]);
    }
    return validate(result);
  }
  function saveBrowser(values,read,write,remove){
    values=validate(values);const previous=new Map();
    try{
      for(const key of Object.keys(values)) previous.set(key,read(key));
      for(const [key,value] of Object.entries(values)) write(key,value);
    }catch(e){
      let restored=true;
      for(const [key,value] of previous){try{if(value===null) remove(key);else write(key,value);}catch(e){restored=false;}}
      throw new Error(restored?'浏览器偏好保存失败；已保留旧值':
        '浏览器偏好保存及恢复失败；请检查现有偏好，未报告导入成功');
    }
    return values;
  }
  const api={FORMAT,MAX_BYTES,KEYS,validate,readValues,exportText,parseImport,merge,saveBrowser};
  if(typeof module!=='undefined'&&module.exports) module.exports=api;
  root.SBPreferences=api;
})(typeof globalThis!=='undefined'?globalThis:this);
