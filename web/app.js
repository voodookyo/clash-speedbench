// Clash SpeedBench v0.6 前端：零依赖原生 SPA
// 三视图 hash 路由（#/nodes #/history #/about），视图常驻 DOM 只切 display，
// 测速轮询为全局单例，切视图不中断；boot 时若已有任务在跑（菜单栏触发/页面刷新）则接管续播。
// 纪律：无 inline 事件属性；节点名一律经 esc() + data-name/dataset 传递，绝不拼进 JS 源码。
"use strict";

/* ==================== 基础 ==================== */

// 写操作令牌：由服务端每次启动随机生成并注入 <meta>，所有 POST 必须携带
const SB_TOKEN = (document.querySelector('meta[name="sb-token"]') || {}).content || '';

async function post(url, body){
  const r = await fetch(url, {method:'POST',
    headers:{'X-SpeedBench-Token': SB_TOKEN, 'Content-Type':'application/json'},
    body: JSON.stringify(body||{})});
  return r.json();
}

async function getJSON(url){
  const r = await fetch(url);
  if(r.ok===false) throw new Error('本实例暂时无法读取该数据');
  return r.json();
}

// 一切进入 innerHTML 的动态文本必须过 esc
function esc(s){ return (s??'').toString().replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

// localStorage 在某些隐私模式下会抛异常，包一层静默降级
const SB_DESKTOP = window.SPEEDBENCH_ENV?.client==='webview';
let desktopPreferences={},preferenceWrites=Promise.resolve(),desktopPreferencesReady=!SB_DESKTOP;
function lsGet(k){ if(SB_DESKTOP) return desktopPreferences[k]??null;try{ return localStorage.getItem(k); }catch(e){ return null; } }
function lsSet(k,v){
  if(SB_DESKTOP){
    desktopPreferences[k]=v;
    // Ordered patches avoid two quick favorite/theme edits racing to disk.
    preferenceWrites=preferenceWrites.then(()=>post('/api/preferences',{[k]:v})).then(r=>{
      if(!r.ok) toast('偏好未能保存；本次界面暂时保留，重启后可能丢失',false);
    }).catch(()=>toast('偏好保存连接失败',false));
  }else try{ localStorage.setItem(k,v); }catch(e){}
}

function clamp(v, lo, hi){ return Math.max(lo, Math.min(hi, v)); }

/* toast：右上滑入，成功绿/失败红，3 秒消失 */
function toast(msg, ok=true){
  if(typeof document.createElement !== 'function') return;
  const box = document.getElementById('toasts');
  if(!box) return;
  const el = document.createElement('div');
  el.className = 'toast ' + (ok ? 'ok' : 'err');
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(()=>{ el.classList.add('show'); }, 20);
  setTimeout(()=>{ el.classList.remove('show'); setTimeout(()=>{ el.remove(); }, 300); }, 3000);
}

/* 自制确认对话框（中断测速/停止面板这类破坏性操作用） */
let modalYes = null, modalReturnFocus=null;
function confirmModal(text, onYes, returnFocus=document.activeElement){
  modalReturnFocus=returnFocus;
  document.getElementById('modal-text').textContent = text;
  modalYes = onYes;
  document.getElementById('modal-mask').style.display = 'flex';
  const no=document.getElementById('modal-no');if(no.focus) no.focus();
}
function closeModal(){
  document.getElementById('modal-mask').style.display = 'none';
  modalYes = null;
  if(modalReturnFocus?.dataset?.nodeId && !modalReturnFocus.isConnected){
    const id=modalReturnFocus.dataset.nodeId;
    modalReturnFocus=[...document.querySelectorAll('button.sw')].find(b=>b.dataset.nodeId===id)||null;
  }
  if(modalReturnFocus && modalReturnFocus.isConnected && modalReturnFocus.focus) modalReturnFocus.focus({preventScroll:true});
  modalReturnFocus=null;
}

/* ==================== 评分 Profile（公式与旧版一致，勿改） ==================== */
// all=综合推荐(后端 score 原样) / daily=⚡日常 / download=🚀下载 /
// ipclean=🧼IP / residential=🏠住宅优先。下载 Profile 不读取 IP 规则。
const PROFILES = ['all','daily','download','ipclean','residential'];
let currentProfile = lsGet('sb_profile');
if(!PROFILES.includes(currentProfile)) currentProfile = 'all';

function profileScore(r){ return SBProfiles.score(r,currentProfile); }

// 切换评分 Profile：存 localStorage、高亮选中按钮、自动按新 Profile 分数降序
function setProfile(p){
  currentProfile = p;
  lsSet('sb_profile', p);
  const btns = document.querySelectorAll('#profile-bar .pf');
  for(const b of btns){ if(b.classList) b.classList.toggle('on', b.dataset.p===p); }
  sortKey='score'; sortAsc=false; updateSortArrows('th.sort', sortKey, sortAsc);
  renderTable(); renderBoard();
}

/* ==================== 收藏 ==================== */
// 收藏节点集：localStorage 持久化；只影响 ★ 展示和地区榜顶部速览，不干扰表格排序
let favs;
try{ favs = new Set(JSON.parse(lsGet('sb_favs')||'[]')); }
catch(e){ favs = new Set(); }

function toggleFav(name){
  if(favs.has(name)) favs.delete(name); else favs.add(name);
  lsSet('sb_favs', JSON.stringify([...favs]));
  renderTable(); renderBoard();
}

/* ==================== 标签 / IP 画像 ==================== */
function tagHtml(tags){
  if(!tags) return '-';
  return tags.split(',').map(t=>{
    let cls='tag';
    if(/不通|龟速|脏IP|高延迟/.test(t)) cls+=' bad';
    if(/低延迟|高带宽|ISP\/非托管/.test(t)) cls+=' good';
    return `<span class="${cls}">${esc(t)}</span>`;
  }).join('');
}

// 旧历史记录的 kind 取值（住宅/机房/移动）映射到新口径，保证老数据不崩
const INTEL_KIND_LABEL = {
  residential: '住宅 ISP',
  residential_proxy: 'ISP住宅代理',
  corporate: '企业/商宽',
  mobile: '移动网络',
  datacenter: '数据中心',
  vpn_proxy: '代理/VPN',
  unknown: '未知',
};
function normKind(ip){ return SBProfiles.normKind(ip); }
function intelOf(r,preferred){ return SBProfiles.intelOf(r,preferred); }
function classificationOf(r,preferred){ return SBProfiles.classificationOf(r,preferred); }
function classificationLabel(r){
  const x = intelOf(r), c = classificationOf(r);
  const confidence = x.confidence ?? (x.classification && x.classification.confidence);
  let text = INTEL_KIND_LABEL[c] || normKind(r && r.ip);
  if(c==='residential' && confidence!=null)
    text = (Number(confidence)>=80 ? '高置信度住宅 ISP' : '疑似住宅');
  return text || '未知';
}
function ipGradeOf(r){
  const x = intelOf(r);
  return x.ip_grade || x.grade || r.ip_grade || '-';
}
function ipRiskOf(r){
  const x = intelOf(r);
  const values = [];
  if(x.ipqs_fraud_score!=null) values.push(`IPQS ${esc(x.ipqs_fraud_score)}`);
  if(x.scamalytics_score!=null) values.push(`Scam ${esc(x.scamalytics_score)}`);
  if(x.scamalytics_risk) values.push(esc(x.scamalytics_risk));
  if(x.ip_quality_score!=null && !values.length) values.push(`Grade ${esc(x.ip_quality_score)}`);
  return values.length ? values.join(' · ') : (r && r.risk ? esc(r.risk) : '-');
}

function ipHtml(ip){
  if(!ip || !ip.ok) return '-';
  let h = `${esc(ip.country_code||ip.country||'?')}·${esc(normKind(ip))}`;
  const badges = [];
  if(ip.proxy)   badges.push('<span class="tag bad">代理</span>');
  if(ip.hosting) badges.push('<span class="tag bad">托管</span>');
  if(ip.mobile)  badges.push('<span class="tag">移动</span>');
  return badges.length ? h + ' ' + badges.join('') : h;
}

function ipDisplayHtml(r){
  const intel = intelOf(r);
  if(intel && (intel.ip || intel.classification || intel.ip_grade)){
    const country = intel.country_code || intel.country || '?';
    const ip = intel.ip ? ` <span class="mono">${esc(intel.ip)}</span>` : '';
    return `${esc(country)}·${esc(classificationLabel(r))}${ip}`;
  }
  return ipHtml(r && r.ip);
}

/* ==================== 地区启发式（与旧版一致，勿改） ==================== */
// 优先级：1) IP 画像 country_code（实测出口，最可靠）；
// 2) 节点名开头国旗 emoji：地区指示符（U+1F1E6–U+1F1FF）两两一对，减偏移得字母代码；
// 3) 节点名关键词：二位大写缩写带 \b 边界匹配（防 "Plus" 误中 "US"），再加常见中文地名；
// 4) 都认不出归 '??' 组。
function flagCode(name){
  const cps = [...name];             // 按码点展开，避开 UTF-16 代理对问题
  if(cps.length<2) return null;
  const a = cps[0].codePointAt(0), b = cps[1].codePointAt(0);
  if(a>=0x1F1E6 && a<=0x1F1FF && b>=0x1F1E6 && b<=0x1F1FF)
    return String.fromCharCode(65+a-0x1F1E6) + String.fromCharCode(65+b-0x1F1E6);
  return null;
}
const REGION_KEYS = [
  [/\b(HK|HKG)\b|香港/, 'HK'], [/\b(TW|TWN)\b|台湾|台北/, 'TW'],
  [/\b(JP|JPN)\b|日本|东京|大阪/, 'JP'], [/\b(SG|SGP)\b|新加坡|狮城/, 'SG'],
  [/\b(US|USA)\b|美国|洛杉矶|圣何塞|西雅图|纽约/, 'US'],
  [/\b(KR|KOR)\b|韩国|首尔/, 'KR'], [/\b(UK|GB|GBR)\b|英国|伦敦/, 'GB'],
  [/\b(DE|DEU)\b|德国|法兰克福/, 'DE'], [/\b(FR|FRA)\b|法国|巴黎/, 'FR'],
  [/\b(CA|CAN)\b|加拿大|多伦多/, 'CA'], [/\b(AU|AUS)\b|澳大利亚|澳洲|悉尼/, 'AU'],
  [/\b(NL|NLD)\b|荷兰|阿姆斯特丹/, 'NL'], [/\b(IN|IND)\b|印度|孟买/, 'IN'],
  [/\b(RU|RUS)\b|俄罗斯|莫斯科/, 'RU'], [/\b(TR|TUR)\b|土耳其|伊斯坦布尔/, 'TR'],
  [/\b(MY|MYS)\b|马来西亚|吉隆坡/, 'MY'], [/\b(TH|THA)\b|泰国|曼谷/, 'TH'],
  [/\b(PH|PHL)\b|菲律宾|马尼拉/, 'PH'], [/\b(VN|VNM)\b|越南|河内/, 'VN'],
  [/\b(ID|IDN)\b|印尼|雅加达/, 'ID'], [/\b(CN|CHN)\b|中国|大陆/, 'CN'],
];
function keywordCode(name){
  for(const [re, code] of REGION_KEYS){ if(re.test(name)) return code; }
  return null;
}
function regionOf(r){
  const ip = r.ip;
  if(ip && ip.ok && ip.country_code) return ip.country_code.toUpperCase();
  return flagCode(r.name||'') || keywordCode(r.name||'') || '??';
}

/* ==================== 排序 ==================== */
// IP 列按类型固定优先级排序：ISP/非托管 > 移动网络 > 机房托管 > 代理/VPN > 未知。
// 数值越大排越前（表格默认降序）；未识别的旧值按"未知"处理；
// 查询失败（无 ip 或 !ok）返回 null，由排序逻辑统一沉底
const KIND_RANK = {'ISP/非托管':4,'移动网络':3,'机房托管':2,'代理/VPN':1,'未知':0};
function sortVal(r, k){
  if(k==='ip'){ const ip=r.ip; return (ip&&ip.ok)?(KIND_RANK[normKind(ip)]??0):null; }
  if(k==='score') return profileScore(r);  // 评分列的排序键跟随当前 Profile
  return r[k];
}

// 通用排序：null（不通/无数据）永远沉底
function sortRows(rows, key, asc){
  rows.sort((a,b)=>{
    if(key==='score')return SBProfiles.compare(a,b,currentProfile,asc);
    const va=sortVal(a,key), vb=sortVal(b,key);
    if(va==null && vb==null) return 0;
    if(va==null) return 1;
    if(vb==null) return -1;
    const cmp = (typeof va==='string') ? va.localeCompare(vb,'zh') : va-vb;
    return asc ? cmp : -cmp;
  });
}

function updateSortArrows(sel, key, asc){
  const ths = document.querySelectorAll(sel);
  for(const th of ths){
    const arr = th.querySelector('.arr');
    const active = !!(th.dataset && th.dataset.k===key);
    if(arr) arr.textContent = active ? (asc?'▲':'▼') : '';
    if(typeof th.setAttribute==='function')
      th.setAttribute('aria-sort', active ? (asc?'ascending':'descending') : 'none');
  }
}

// 列表按稳定标识重渲染后把键盘焦点还给当前选中项；逐项比较 dataset，
// 绝不把可能含引号的订阅名拼进选择器。
function restoreListFocus(boxId, selector, attr, value){
  const box = document.getElementById(boxId);
  if(!box || typeof box.querySelectorAll!=='function') return;
  for(const el of box.querySelectorAll(selector)){
    if(el && el.dataset && String(el.dataset[attr])===String(value) && typeof el.focus==='function'){
      el.focus({preventScroll:true}); return;
    }
  }
}

/* ==================== 全局状态 ==================== */
let latestData = null;        // null=加载中（骨架屏）；{}=无记录
let sortKey = 'score', sortAsc = false;
let currentNode = '', currentGroup = '';
let searchText = '';
let expandedNode = null;      // 节点视图中展开详情面板的节点（一次只展开一个）
let pollTimer = null;         // 测速状态轮询：全局单例，切视图不清除
let sourceCatalog = {nodes:[],sources:[]}, selectedNodeIds = new Set();
let rootControls=null;
let catalogRequestRevision=0;
let taskClient = null, taskConfig = null, activeTask = null;
let favIds = new Set();
try{ favIds=new Set(JSON.parse(lsGet('sb_favs_v2')||'[]')); }catch(e){}
const taskLabels={queued:'排队中',preparing:'准备中',probing:'探测中',measuring:'带宽精测中',enriching:'IP 画像查询中',finalizing:'保存与清理中',cancelling:'正在取消与清理',completed:'已完成',cancelled:'已取消 · 部分结果',failed:'失败 · 部分结果',interrupted:'应用中断 · 部分结果'};
function nodeUiKey(r){ return r.node_id || r.name; }
function sourceLabel(r){ return r.subscription_name || (r.source_status==='ambiguous'?'多个来源（无法唯一确认）':r.provider||'来源未知'); }

async function loadSourceCatalog(){
  const stamp=++catalogRequestRevision;
  const select = document.getElementById('f-source');
  const status = document.getElementById('source-status');
  if(!select || !status) return;
  const selected = select.value || '';
  try{
    const catalog = await getJSON('/api/catalog');
    if(stamp!==catalogRequestRevision) return;
    sourceCatalog = catalog;
    if(typeof SBTasks!=='undefined'){
      const migrated=SBTasks.migrateFavorites([...favs],catalog.nodes||[],[...favIds]);
      favIds=new Set(migrated.ids); favs=new Set(migrated.pending);
      lsSet('sb_favs_v2',JSON.stringify([...favIds])); lsSet('sb_favs',JSON.stringify([...favs]));
      updatePendingFavorites();
    }
    select.innerHTML = '<option value="">全部已加载节点</option>' + (catalog.sources||[]).map(s=>
      `<option value="${esc(s.subscription_id)}"${s.loaded?'':' disabled'}>${esc(s.name)}${s.loaded?'':' · 未加载/不可用'}</option>`).join('');
    if(typeof SBTasks!=='undefined') select.innerHTML+='<option value="__favorites__">已收藏节点</option><option value="__manual__">手动选择节点</option>';
    const valid = ['__favorites__','__manual__'].includes(selected) || (catalog.sources||[]).some(s=>s.loaded && s.subscription_id===selected);
    if(selected && !valid){
      select.innerHTML += `<option value="${esc(selected)}" disabled>原选订阅已失效 · 请重新选择</option>`;
    }
    select.value = selected;
    const unknown = (catalog.nodes||[]).filter(n=>n.source_status!=='verified').length;
    status.textContent = catalog.status==='ok'
      ? ((catalog.nodes||[]).length?`已核验 ${catalog.nodes.length} 个节点 · ${unknown} 个来源不唯一/未知。未加载订阅不会自动切换。`:'当前没有已加载节点。请在 Verge 加载订阅，再点击“刷新订阅”。')
      : '订阅目录暂不可核验。请启动 Verge、开启外部控制器，再刷新订阅；来源不会被猜测填入。';
    renderLiveSubsCatalog();
    renderNodePicker(); updateTaskBudget();
  }catch(e){
    if(stamp===catalogRequestRevision){
      sourceCatalog={status:'unavailable',nodes:[],sources:[]};
      status.textContent='订阅目录读取失败或超时；请确认 Verge 已运行、外部控制器可用，再刷新订阅。';
      renderLiveSubsCatalog();renderNodePicker();updateTaskBudget();
    }
  }
}

/* ==================== 表格行渲染（节点/历史/订阅三视图复用） ==================== */
// opts: {readonly, currentNode, favs, expanded, selected, provider, cols}
function rowHtml(r, i, opts){
  const ro = opts.readonly;
  const isCur = !ro && r.name===opts.currentNode;
  const isFav = !ro && (r.node_id?favIds.has(r.node_id):opts.favs.has(r.name));
  const sc = profileScore(r);
  // 评分列 = 当前 Profile 分数（一位小数）+ 星标。星标始终显示后端 stars：
  // stars 是后端综合评级的直观符号，Profile 切换只改数值与排序，不同步换算以免误导。
  let h = `<tr data-name="${esc(r.name)}" data-node-id="${esc(r.node_id||'')}" data-row-key="${esc(nodeUiKey(r))}" tabindex="0"${ro?'':` aria-expanded="${!!opts.expanded}"`}${isCur?' class="current"':''}${opts.selected?' class="sel"':''}>`;
  h += `<td>${i+1}</td><td>`;
  if(!ro)
    h += `<button class="fav${isFav?' on':''}" data-name="${esc(r.name)}" data-node-id="${esc(r.node_id||'')}" aria-label="收藏/取消收藏 ${esc(r.name)}" aria-pressed="${isFav}">${isFav?'★':'☆'}</button>`;
  h += esc(r.name);
  if(isCur) h += '<span class="cur-mark">✅ 使用中</span>';
  h += `<div class="node-source">${esc(sourceLabel(r))}</div></td>`;
  if(opts.provider) h += `<td class="mono">${esc(r.provider||'(未知订阅)')}</td>`;
  h += `<td class="mono">${r.latency_ms??'-'}</td>`;
  h += `<td class="mono">${r.median_mbps?r.median_mbps.toFixed(1):'-'}</td>`;
  const scope=(r.measurement_scope||{}).bandwidth;
  const network = ['not_requested','not_selected','pending'].includes(scope)?null:(r.network_score ?? r.networkScore ?? r.score);
  h += `<td class="stars" data-name="${esc(r.name)}" title="查看 30 天趋势"><span class="sc-num">${network==null?'-':Number(network).toFixed(1)}</span> ${esc(r.stars||'')}</td>`;
  if(opts.intelColumns !== false){
    h += `<td class="mono">${esc(ipGradeOf(r))}</td>`;
    h += `<td>${esc(classificationLabel(r))}</td>`;
    h += `<td>${ipRiskOf(r)}</td>`;
  }else{
    h += `<td>${ipDisplayHtml(r)}</td>`;
  }
  h += `<td>${tagHtml(r.tags)}</td><td>`;
  if(!ro)
    h += isCur ? '<button class="mini" disabled>使用中</button>'
               : `<button class="mini sw" data-name="${esc(r.name)}" data-node-id="${esc(r.node_id||'')}">切换</button>`;
  h += '</td></tr>';
  if(opts.expanded) h += detailHtml(r, opts.cols||8);
  return h;
}

// 行展开详情：延迟/抖动/建连/样本/单流/多流 + 出口 IP/ASN/ISP + 趋势入口
// 结果元数据展示：只显示真实的观测完成时间与独立状态；旧记录一律 unknown，
// 绝不把缺失字段显示成 now/0，也不从事件名推断成功。
const SB_SCOPE_REASON = {completed:'完成',partial:'部分完成',failed:'失败',pending:'等待中',
  cancelled:'已取消',not_requested:'未请求',not_selected:'未选中',interrupted:'已中断',unknown:'未知'};
function metricTimeText(r,key){
  const stamps=(r&&typeof r.metric_updated_at==='object'&&r.metric_updated_at)||{};
  const t=stamps[key];
  if(!Number.isSafeInteger(t)||t<0||t>253402300799999) return '未知';
  const d=new Date(t);
  if(isNaN(d.getTime())) return '未知';
  const p=n=>String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function scopeStateText(r,key){
  const scope=(r&&typeof r.measurement_scope==='object'&&r.measurement_scope)||{};
  const raw=scope[key];
  const status=(typeof raw==='string'&&raw)?raw:'unknown';
  return Object.prototype.hasOwnProperty.call(SB_SCOPE_REASON,status)?SB_SCOPE_REASON[status]:'未知';
}
function measuredCountText(r){
  const n=r?r.measured_metric_count:null;
  return (Number.isSafeInteger(n)&&n>=0&&n<=9)?String(n):'未知';
}
function detailHtml(r, colspan){
  const ip = r.ip || {};
  const intel = intelOf(r);
  const cls = intel.classification && typeof intel.classification === 'object'
    ? intel.classification : {};
  const asnValue = intel.asn || ip.asn;
  const asnName = intel.as_name || intel.asname || ip.asname;
  const asn = asnValue ? ('AS'+String(asnValue).replace(/^AS/i,'')) : '';
  const asnTxt = asn ? esc(asn + (asnName?' '+asnName:'')) : '-';
  const isp = intel.isp || ip.isp;
  const organization = intel.organization || intel.org || ip.org;
  const cell = (k,v)=>`<div><div class="k">${k}</div><div class="v">${v}</div></div>`;
  const cells = [
    cell('抖动', r.jitter_ms!=null ? esc(r.jitter_ms)+' ms' : '-'),
    cell('建连', r.connect_ms!=null ? esc(r.connect_ms)+' ms' : '-'),
    cell('样本大小', r.sample_mb!=null ? esc(r.sample_mb)+' MB' : '-'),
    cell('单流带宽（中位）', r.median_mbps!=null ? r.median_mbps.toFixed(1)+' Mbps' : '-'),
    cell('单流带宽（最佳）', r.best_mbps!=null ? r.best_mbps.toFixed(1)+' Mbps' : '-'),
    cell('多流带宽', r.multi_mbps!=null ? r.multi_mbps.toFixed(1)+' Mbps' : '-'),
    cell('Network Score', r.network_score!=null ? esc(r.network_score) : (r.score!=null ? esc(r.score) : '-')),
    cell('Network 更新', metricTimeText(r,'network')),
    cell('IP Grade 更新', metricTimeText(r,'ip_grade')),
    cell('探测状态', esc(scopeStateText(r,'probe'))),
    cell('带宽状态', esc(scopeStateText(r,'bandwidth'))),
    cell('情报状态', esc(scopeStateText(r,'intel'))),
    cell('已测指标数', measuredCountText(r)),
    cell('应用层探测失败率', r.probe_loss_pct!=null ? esc(r.probe_loss_pct)+'%' : '-'),
    cell('出口 IP', ip.ok ? esc(ip.exit_ip||'-') : '-'),
    cell('IPv4', esc(r.exit_ipv4 || intel.exit_ipv4 || (ip.ok ? ip.exit_ip : '') || '-')),
    cell('IPv6', esc(r.exit_ipv6 || intel.exit_ipv6 || '-')),
    cell('IPv4 状态', esc(scopeStateText({measurement_scope:r.exit_status},'ipv4'))),
    cell('IPv6 状态', esc(scopeStateText({measurement_scope:r.exit_status},'ipv6'))),
    cell('ASN', asnTxt),
    cell('ISP', isp ? esc(isp) : '-'),
    cell('组织', organization ? esc(organization) : '-'),
    cell('IP 类型', esc(classificationLabel(r))),
    cell('Confidence', cls.confidence!=null ? esc(cls.confidence)+'%' : '-'),
    cell('IP Grade', esc(ipGradeOf(r))),
    cell('IPQS Fraud', intel.ipqs_fraud_score!=null ? esc(intel.ipqs_fraud_score) : '-'),
    cell('Scamalytics Fraud', intel.scamalytics_score!=null ? esc(intel.scamalytics_score) : '-'),
  ].join('');
  const probeCells=Object.entries({main:'主实例',worker:'worker ',serial:'串行'}).map(([key,label])=>{
    const data=r.probe_sources?.[key];
    if(!data || typeof data!=='object') return '';
    return cell(`${label}应用层探测`,`${esc(data.successes??'-')} 成功 / ${esc(data.failures??'-')} 失败<br>`+
      `已完成 ${esc(data.attempts??'-')} / 请求 ${esc(data.requested??'-')}，已调用 ${esc(data.started??'-')}<br>`+
      `${data.status==='completed'?'完整':'部分'} · 完成样本失败率 ${esc(data.loss_pct??'-')}%`);
  }).join('');
  const probeNote=probeCells?'<p class="card-sub">失败率仅依据已完成的 HTTP/HTTPS 探测，不含取消中未返回的请求；主实例与 worker 不合并，非 ICMP 丢包率。</p>':'';
  const evidence = (cls.evidence||[]).map(x=>`<li>${esc(x)}</li>`).join('');
  const conflicts = (cls.conflicts||[]).map(x=>`<li>${esc(x)}</li>`).join('');
  const pdata = intel.provider_data || intel.providers || {};
  const providerBlocks = Object.entries(pdata).map(([name, data])=>{
    const rows = Object.entries(data||{}).map(([key, value])=>{
      const shown = (value && typeof value === 'object') ? JSON.stringify(value) : value;
      return `<tr><td>${esc(key)}</td><td>${esc(shown==null?'-':shown)}</td></tr>`;
    }).join('');
    return `<details class="provider-detail"><summary>${esc(name)}</summary><table class="compact"><tbody>${rows||'<tr><td colspan="2">N/A</td></tr>'}</tbody></table></details>`;
  }).join('');
  const intelText = `<div class="intel-detail"><b>Evidence</b><ul>${evidence||'<li>-</li>'}</ul>`+
    `<b>Conflicts</b><ul>${conflicts||'<li>-</li>'}</ul>`+
    `<div class="card-sub">Provider 状态：${esc(JSON.stringify(intel.provider_status||{}))}</div>`+
    `<div class="provider-detail-list">${providerBlocks||'<span class="card-sub">暂无 Provider 细节</span>'}</div></div>`;
  return `<tr class="detail-row"><td colspan="${colspan||8}"><div class="detail-grid">${cells}${probeCells}</div>${probeNote}${intelText}` +
         `<div class="detail-actions"><button class="mini trend" data-name="${esc(r.name)}" data-node-id="${esc(r.node_id||'')}">📈 查看 30 天趋势</button></div></td></tr>`;
}

function skeletonRows(n, colspan){
  let h = '';
  for(let i=0;i<n;i++) h += `<tr class="skel-row"><td colspan="${colspan||8}"><div class="skel"></div></td></tr>`;
  return h;
}

function emptyRow(text, colspan){
  return `<tr class="empty-row"><td colspan="${colspan||8}">${esc(text)}</td></tr>`;
}

/* ==================== 节点视图 ==================== */
function renderTable(){
  const tbody = document.getElementById('tbody');
  if(!latestData){ tbody.innerHTML = skeletonRows(6); return; }
  if(activeTask && latestData.task===activeTask.config && latestData.ts===activeTask.started_at) renderTaskResultMeta(activeTask);
  const all = latestData.results || [];
  if(!all.length){
    tbody.innerHTML = emptyRow(activeTask && !SBTasks.terminal(activeTask.status)?'等待首个探测结果；任务正在运行。':'暂无测速记录 · 在上方选择范围与模式，点击「开始测速」');
    return;
  }
  // 有任一节点带订阅来源时才显示「订阅」列（旧历史没有 provider 字段）
  const showProv = all.some(r=>r.provider);
  const pth = document.getElementById('th-provider');
  if(pth && pth.style) pth.style.display = showProv ? '' : 'none';
  const cols = showProv ? 11 : 10;
  const q = searchText.trim().toLowerCase();
  const rows = all.filter(r=>!q || (r.name||'').toLowerCase().includes(q)
                        || (r.provider||'').toLowerCase().includes(q));
  if(!rows.length){
    tbody.innerHTML = emptyRow(`没有匹配「${searchText.trim()}」的节点`, cols);
    return;
  }
  sortRows(rows, sortKey, sortAsc);
  const render=()=>{tbody.innerHTML = rows.map((r,i)=>rowHtml(r, i, {
    readonly:false, currentNode, favs,
    expanded: expandedNode===nodeUiKey(r), selected:false,
    provider: showProv, cols,
  })).join('');};
  if(typeof SBView!=='undefined') SBView.retainTable(tbody,render);else render();
}

function renderMeta(){
  document.getElementById('cur-line').textContent =
    currentNode ? `当前：${currentGroup} = ${currentNode}` : '';
  document.getElementById('cur-line').title=currentNode;
  renderTable();
}

function setSort(k){
  if(sortKey===k){ sortAsc=!sortAsc; } else { sortKey=k; sortAsc=(k==='name'||k==='latency_ms'); }
  updateSortArrows('th.sort', sortKey, sortAsc);
  renderTable();
}

/* ---------- 地区榜 ---------- */
function boardItem(x){
  return `<button type="button" class="board-item" data-name="${esc(x.name)}" data-node-id="${esc(x.node_id||'')}">${esc(x.name)} <b>${x.sc==null?'-':x.sc.toFixed(1)}</b>${x.mbps!=null?`·${x.mbps.toFixed(0)}M`:''}</button>`;
}

// 地区榜：按 regionOf 分组，每组取当前 Profile 下 Top 3；不通/无数据（分数 null）不进榜；
// 顶部固定一行「⭐ 收藏」速览（无收藏时不显示）；整榜在无数据时隐藏
function renderBoard(){
  const bd = document.getElementById('board');
  if(!latestData || !latestData.results || !latestData.results.length){ bd.style.display='none'; return; }
  let html = '';
  const favRows = latestData.results.filter(r=>r.node_id?favIds.has(r.node_id):favs.has(r.name))
    .sort((a,b)=>SBProfiles.compare(a,b,currentProfile))
    .map(r=>({name:r.name, node_id:r.node_id, sc:profileScore(r), mbps:r.median_mbps}));
  if(favRows.length)
    html += `<div class="board-group"><span class="board-code">⭐ 收藏</span>${favRows.map(boardItem).join('')}</div>`;
  const groups = {};
  for(const r of latestData.results){
    const sc = profileScore(r);
    if(sc==null) continue;
    const code = regionOf(r);
    if(!groups[code]) groups[code] = [];
    groups[code].push({...r,sc,mbps:r.median_mbps});
  }
  const codes = Object.keys(groups).sort((a,b)=>{
    const top = c=>Math.max(...groups[c].map(x=>x.sc));
    return top(b)-top(a) || a.localeCompare(b);
  });
  for(const c of codes){
    const top3 = groups[c].sort((a,b)=>SBProfiles.compare(a,b,currentProfile)).slice(0,3);
    html += `<div class="board-group"><span class="board-code">${esc(c)}</span>${top3.map(boardItem).join('')}</div>`;
  }
  if(!html){ bd.style.display='none'; return; }
  bd.style.display='';
  document.getElementById('board-body').innerHTML = html;
}

/* ==================== 历史视图 ==================== */
let histData = [];
let histLoaded = false;
let histSortKey = 'score', histSortAsc = false;
let histSelRun = -1;          // histData 下标；-1=未选
let histSelNode = null, histSelNodeId = '';
// 单节点 30 天趋势：{name, pts:[{ts,v}], changes:[...], reputation_changes:[...], note}；
// name 不符时回退 histData
let nodeTrend = null;

// 新轮次按保存的使用目标及测量覆盖推荐；旧轮次保留综合评分口径。
function championOf(rec){
  if(rec.task || (rec.results||[]).some(r=>r.measurement_scope?.mode)){
    const profile=rec.task?.target_profile||'balanced';
    return (rec.results||[]).filter(r=>SBProfiles.score(r,profile)!=null)
      .sort((a,b)=>SBProfiles.compare(a,b,profile))[0]||null;
  }
  let best = null;
  for(const r of (rec.results||[])){
    if(r.score==null) continue;
    if(!best || r.score>best.score) best = r;
  }
  return best;
}

async function loadHistory(){
  const selectedStamp=histData[histSelRun]?.ts;
  try{ histData = await getJSON('/api/history'); }
  catch(e){ histData = []; toast('读取历史记录失败', false); }
  if(selectedStamp) histSelRun=histData.findIndex(r=>r.ts===selectedStamp);
  histLoaded = true;
  if(histData.length && (histSelRun<0 || histSelRun>=histData.length)){
    histSelRun = histData.length-1;   // 默认最新一轮 + 冠军节点
    const ch = championOf(histData[histSelRun]);
    const selected=ch || (histData[histSelRun].results||[])[0];
    histSelNode=selected?selected.name:null;
    histSelNodeId=selected?selected.node_id||'':'';
  }
  renderHistList(); renderHistTable();
  if(histSelNode) fetchNodeTrend(histSelNode); else drawChart();
}

function renderHistList(){
  const box = document.getElementById('hist-list');
  if(!histData.length){
    box.innerHTML = '<div class="hist-empty">暂无历史测速记录<br>先在「节点」页跑一轮测速</div>';
    return;
  }
  let html = '';
  for(let i=histData.length-1;i>=0;i--){   // 最新在上
    const rec = histData[i];
    const n = (rec.results||[]).length;
    const ch = championOf(rec);
    const sub = ch ? `${n} 节点 · 🥇 ${esc(ch.name)} · ${ch.median_mbps!=null?ch.median_mbps.toFixed(1)+'M':'-'}`
                   : `${n} 节点`;
    html += `<button type="button" class="hist-item${i===histSelRun?' on':''}" data-i="${i}"${i===histSelRun?' aria-current="true"':''}>` +
            `<span class="hist-ts">${esc(rec.ts||'')}</span><span class="hist-sub">${sub}</span></button>`;
  }
  box.innerHTML = html;
}

function renderHistTable(){
  const tbody = document.getElementById('hist-tbody');
  const rec = histData[histSelRun];
  const meta=document.getElementById('hist-run-meta');
  if(meta)meta.textContent='';
  if(!rec){ tbody.innerHTML = emptyRow('暂无数据'); return; }
  document.getElementById('hist-run-title').textContent = `本轮结果：${rec.ts}（只读，点击行看趋势）`;
  if(meta && typeof SBHistory!=='undefined'){
    const summary=SBHistory.describe(rec);
    meta.textContent=`${summary.mode} · ${summary.status}\n${summary.range} · 耗时 ${summary.elapsed} · 流量 ${summary.traffic}\n指标覆盖（按已返回节点）：${summary.coverage}`;
  }
  const rows = (rec.results||[]).slice();
  if(!rows.length){ tbody.innerHTML = emptyRow('该轮没有节点数据'); return; }
  sortRows(rows, histSortKey, histSortAsc);
  const render=()=>{tbody.innerHTML = rows.map((r,i)=>rowHtml(r, i, {
    readonly:true, currentNode:'', favs:{has(){return false}},
    expanded:false, selected: histSelNodeId?r.node_id===histSelNodeId:!r.node_id && r.name===histSelNode, intelColumns:false,
  })).join('');};
  if(typeof SBView!=='undefined') SBView.retainTable(tbody,render);else render();
}

function setHistSort(k){
  if(histSortKey===k){ histSortAsc=!histSortAsc; } else { histSortKey=k; histSortAsc=(k==='name'||k==='latency_ms'); }
  updateSortArrows('th.hsort', histSortKey, histSortAsc);
  renderHistTable();
}

/* ---------- 单节点 30 天趋势 + IP 变化 ---------- */
// 数据源：优先 /api/node 的 30 天序列（含 IP 变化时间线），失败回退 histData
async function fetchNodeTrend(name,nodeId=histSelNodeId){
  nodeTrend = null;
  drawChart();   // 先用 histData 画兜底版
  renderIpTimeline();
  try{
    const d = await getJSON('/api/node?name='+encodeURIComponent(name)+'&days=30'+(nodeId?'&node_id='+encodeURIComponent(nodeId):''));
    if(histSelNode!==name || histSelNodeId!==nodeId) return;
    nodeTrend = {
      name,node_id:nodeId,
      pts: (d.series||[]).filter(s=>s.median_mbps!=null)
           .map(s=>({ts:(s.ts||'').slice(5,16), v:s.median_mbps})),
      changes: d.ip_changes||[],
      reputation_changes: Array.isArray(d.ip_reputation_changes)
        ? d.ip_reputation_changes : [],
      note: ipChangeNote(d.ip_changes),
    };
  }catch(e){
    if(histSelNode!==name || histSelNodeId!==nodeId) return;
    nodeTrend = {name,node_id:nodeId, pts:[], changes:[], reputation_changes:[], note:''};
  }
  drawChart(); renderIpTimeline();
}

// ip_changes 是「相邻不变则合并」的变化点时间线：首条是初始 IP，之后每条算一次变化
function ipChangeNote(changes){
  if(!changes || changes.length<2) return '';
  const last = changes[changes.length-1], prev = changes[changes.length-2];
  return `出口 IP 曾变化 ${changes.length-1} 次（最近：${prev.exit_ip||'?'} → ${last.exit_ip||'?'} @ ${String(last.ts||'').slice(0,16)}）`;
}

function drawChart(){
  const cv = document.getElementById('chart');
  const dpr = window.devicePixelRatio||1;
  const W = cv.clientWidth*dpr, H = cv.clientHeight*dpr;
  if(!W || !H) return;   // 视图隐藏时 clientWidth=0，跳过；切回时 route() 会重画
  cv.width=W; cv.height=H;
  const ctx = cv.getContext('2d');
  ctx.clearRect(0,0,W,H);
  const name = histSelNode;
  document.getElementById('chart-title').textContent =
    name ? `30 天带宽趋势：${name}` : '30 天带宽趋势（选择节点后展示）';
  const useSeries = nodeTrend && nodeTrend.name===name && nodeTrend.node_id===histSelNodeId && nodeTrend.pts.length;
  document.getElementById('chart-sub').textContent = useSeries ? (nodeTrend.note||'') : '';
  const pts = [];
  if(useSeries){
    pts.push(...nodeTrend.pts);
  }else if(name && histData.length){
    for(const rec of histData){
      const r = (rec.results||[]).find(x=>histSelNodeId?x.node_id===histSelNodeId:!x.node_id && x.name===name);
      if(r && r.median_mbps!=null) pts.push({ts:(rec.ts||'').slice(5,16), v:r.median_mbps});
    }
  }
  if(!name) return;
  if(pts.length<1){ ctx.fillStyle='#8b949e'; ctx.font=`${12*dpr}px sans-serif`;
    ctx.fillText('该节点暂无历史数据', 20*dpr, 30*dpr); return; }
  const pad=36*dpr, maxV=Math.max(...pts.map(p=>p.v))*1.15||1;
  const x=i=> pad + (pts.length===1?(W-2*pad)/2:(W-2*pad)*i/(pts.length-1));
  const y=v=> H-pad - (H-2*pad)*v/maxV;
  ctx.strokeStyle='#30363d'; ctx.fillStyle='#8b949e'; ctx.font=`${10*dpr}px sans-serif`;
  for(let g=0; g<=4; g++){ const v=maxV*g/4, yy=y(v);
    ctx.beginPath(); ctx.moveTo(pad,yy); ctx.lineTo(W-pad,yy); ctx.stroke();
    ctx.fillText(v.toFixed(0), 6*dpr, yy+3*dpr); }
  ctx.strokeStyle='#58a6ff'; ctx.lineWidth=2*dpr; ctx.beginPath();
  pts.forEach((p,i)=> i?ctx.lineTo(x(i),y(p.v)):ctx.moveTo(x(i),y(p.v)));
  ctx.stroke();
  ctx.fillStyle='#58a6ff';
  pts.forEach((p,i)=>{ ctx.beginPath(); ctx.arc(x(i),y(p.v),3*dpr,0,7); ctx.fill();
    ctx.fillText(p.v.toFixed(1), x(i)-10*dpr, y(p.v)-8*dpr); });
  ctx.fillStyle='#8b949e';
  pts.forEach((p,i)=>{ if(pts.length<=12||i%2===0) ctx.fillText(p.ts, x(i)-20*dpr, H-10*dpr); });
}

function renderIpTimeline(){
  const box = document.getElementById('ip-timeline');
  const t = nodeTrend && nodeTrend.name===histSelNode && nodeTrend.node_id===histSelNodeId ? nodeTrend : null;
  const changes = t && Array.isArray(t.changes) ? t.changes : [];
  const reputation = t && Array.isArray(t.reputation_changes)
    ? t.reputation_changes : [];
  if(!t || (!changes.length && !reputation.length)){ box.innerHTML=''; return; }
  const items = changes.map(c=>{
    const badges = [];
    if(c.proxy)   badges.push('<span class="tag bad">代理</span>');
    if(c.hosting) badges.push('<span class="tag bad">托管</span>');
    if(c.mobile)  badges.push('<span class="tag">移动</span>');
    const asn = c.asn ? ('AS'+String(c.asn).replace(/^AS/i,'')) : '';
    const who = [asn + (c.asname?' '+c.asname:''), c.isp, c.kind].filter(Boolean).join(' · ');
    return `<div class="ip-item"><span class="ip-ts">${esc(String(c.ts||'').slice(0,16))}</span>` +
           `<span>${esc(c.exit_ip||'?')}${who?' · '+esc(who):''} ${badges.join('')}</span></div>`;
  }).join('');

  // The database keeps this richer timeline separate from legacy
  // ``ip_changes``.  Treat every value as untrusted text before composing the
  // detail HTML; old databases may contain missing or hand-edited fields.
  const repTruthy = v => v===true || v===1 || v==='1' || v==='true';
  const repWorsened = c => repTruthy(c.same_ip_reputation_worsened) ||
                           repTruthy(c.reputation_worsened) ||
                           repTruthy(c.reputation_degraded);
  const repClassLabel = c => {
    const raw = c && c.classification;
    const category = raw && typeof raw==='object' ? raw.category : raw;
    const confidence = c && c.confidence!=null ? Number(c.confidence) : NaN;
    if(category==='residential')
      return Number.isFinite(confidence) && confidence>=80 ? '高置信度住宅 ISP' : '疑似住宅';
    return INTEL_KIND_LABEL[category] || '未知';
  };
  const repValue = v => v==null || v==='' ? '-' : esc(v);
  const repItems = reputation.map(c=>{
    const ip = c.exit_ip || c.exit_ipv4 || c.exit_ipv6 || '?';
    const confidence = c.confidence!=null ? ` · Confidence ${repValue(c.confidence)}%` : '';
    const worsened = repWorsened(c);
    const warning = worsened ? '<span class="tag bad">⚠ 同 IP 信誉明显恶化</span>' : '';
    return `<div class="ip-item rep-item${worsened?' rep-warn':''}">` +
      `<span class="ip-ts">${esc(String(c.ts||'').slice(0,16))}</span>` +
      `<span class="rep-content"><span class="mono">${repValue(ip)}</span>` +
      ` · ${esc(repClassLabel(c))}${confidence}` +
      ` · Grade ${repValue(c.ip_grade || c.grade)}` +
      ` · IPQS ${repValue(c.ipqs_fraud_score)}` +
      ` · Scamalytics ${repValue(c.scamalytics_score)} ${warning}</span></div>`;
  }).join('');
  const deterioration = reputation.some(repWorsened);
  const warningHtml = deterioration
    ? '<div class="leak-warning"><b>⚠ 同一出口 IP 的信誉明显恶化</b>：请对照该 IP 的历史记录与各 Provider 分项指标。</div>'
    : '';
  box.innerHTML =
    (changes.length ? '<div class="card-sub">出口 IP 时间线（相邻不变已合并）</div>' + items : '') +
    (reputation.length ? '<div class="card-sub rep-title">IP Intelligence 历史（按出口 IP）</div>' + warningHtml + repItems : '');
}

/* ==================== 订阅视图 ==================== */
// 按订阅（provider）聚合的历史回顾：汇总表 + 单订阅三线趋势图 + 最近一轮节点表
let subsData = [];          // /api/subscriptions 汇总列表
let subsLoaded = false;
let subsDays = +(lsGet('sb_subs_days')||30) || 30;
let subsSel = null;         // 当前选中的订阅（API 展示名，未知来源为 "(未知订阅)"）
let subsSeries = null;      // {name, pts:[{ts,online_ratio,median_mbps,latency_ms,avg_score}]}

const UNKNOWN_PROVIDER = '(未知订阅)';
// 汇总/API 用展示名，匹配 slim 历史行里的原始 provider 时用原始值
function subsRawProvider(){ return subsSel===UNKNOWN_PROVIDER ? '' : subsSel; }

function renderLiveSubsCatalog(){
  const body=document.getElementById('subs-catalog-tbody'),status=document.getElementById('subs-catalog-status');
  if(!body || !status)return;
  const nodes=sourceCatalog.nodes||[];
  if(sourceCatalog.status!=='ok'){
    status.textContent='实时目录暂不可核验；请检查 Verge 与外部控制器，再刷新目录。下方历史汇总仍可查看。';
    body.innerHTML=emptyRow('实时来源未知；不会按名称猜测归属',4);return;
  }
  status.textContent=`当前已加载 ${nodes.length} 个节点。多来源节点分别出现在相关订阅中，计数不能跨订阅相加。`;
  body.innerHTML=(sourceCatalog.sources||[]).map(s=>{
    const matches=nodes.filter(n=>(n.subscription_ids||[]).includes(s.subscription_id));
    const ambiguous=matches.filter(n=>n.source_status==='ambiguous').length;
    return `<tr><td>${esc(s.name)}</td><td>${s.loaded?'已加载':'未加载/不可用'}</td><td>${matches.length}${ambiguous?`（${ambiguous} 多来源）`:''}</td><td>${s.loaded&&matches.length?`<button type="button" class="mini" data-test-source="${esc(s.subscription_id)}">选择此订阅测速</button>`:'请先在 Verge 加载订阅并刷新目录'}</td></tr>`;
  }).join('');
  const unknown=nodes.filter(n=>n.source_status==='unknown').length;
  if(unknown)body.innerHTML+=`<tr><td>来源未知</td><td>已加载节点</td><td>${unknown}</td><td>在节点页使用全部已加载范围，或手动选择身份明确的节点</td></tr>`;
  if(!body.innerHTML)body.innerHTML=emptyRow('未发现订阅。请在 Verge 加载后刷新目录；可用节点仍可在节点页手动选择。',4);
}

async function loadSubs(){
  loadSourceCatalog(); // Independent live directory failure must not hide history.
  let d;
  try{ d = await getJSON((typeof SBTasks==='undefined'?'/api/subscriptions':'/api/sources/history')+'?days='+subsDays); }
  catch(e){ d = []; toast('读取订阅汇总失败', false); }
  subsData = Array.isArray(d) ? d : [];
  if(typeof SBTasks!=='undefined') subsData=subsData.map(s=>Object.assign({},s,{
    provider:s.name,online_ratio:s.probe_online_ratio,avg_score:s.avg_network_score,
    selection_key:s.subscription_id || 'legacy:'+s.source_status+'|'+s.name}));
  subsLoaded = true;
  renderSubsTable();
  if(subsSel) selectSub(subsSel);   // 天数变化后已选中的订阅也要重拉趋势
}

function renderSubsTable(){
  const tbody = document.getElementById('subs-tbody');
  if(!subsLoaded){ tbody.innerHTML = skeletonRows(3); return; }
  if(!subsData.length){
    tbody.innerHTML = emptyRow('暂无订阅数据 · 先在「节点」页跑一轮测速');
    return;
  }
  tbody.innerHTML = subsData.map(s=>{
    const key=s.selection_key||s.provider, sel=key===subsSel;
    return `<tr data-provider="${esc(key)}"${sel?' class="sel"':''}>` +
    `<td><button type="button" class="subs-pick" data-provider="${esc(key)}" aria-pressed="${sel}">${esc(s.provider)}</button></td>` +
    `<td class="mono">${s.run_count}</td>` +
    `<td class="mono">${s.node_count}</td>` +
    `<td class="mono">${s.online_ratio==null?'N/A':(s.online_ratio*100).toFixed(0)+'%'}${s.bandwidth_coverage==null?'':`<small class="node-source">带宽覆盖 ${(s.bandwidth_coverage*100).toFixed(0)}% · 成功率 ${s.bandwidth_success_ratio==null?'N/A':(s.bandwidth_success_ratio*100).toFixed(0)+'%'}</small>`}</td>` +
    `<td class="mono">${s.median_mbps!=null?s.median_mbps.toFixed(1):'-'}</td>` +
    `<td class="mono">${s.latency_ms!=null?s.latency_ms.toFixed(0):'-'}</td>` +
    `<td class="mono">${s.avg_score!=null?s.avg_score.toFixed(1):'-'}</td>` +
    `<td class="mono">${esc((s.last_ts||'').slice(0,16))}</td></tr>`;
  }).join('');
}

async function selectSub(name){
  subsSel = name;
  const source=subsData.find(s=>(s.selection_key||s.provider)===name);
  const display=source?source.provider:name;
  document.getElementById('subs-detail-card').style.display = '';
  document.getElementById('subs-detail-title').textContent =
    `订阅趋势：${display}（近 ${subsDays} 天，三条线各自归一；多来源节点不能跨订阅相加）`;
  renderSubsTable();
  subsSeries = null;
  drawSubsChart();
  if(source && !source.subscription_id && source.source_status==='unknown'){
    subsSeries={name,pts:[]};
    renderSubsNodes(name);
    return; // No verified source identity: do not merge a legacy empty-name series.
  }
  try{
    const url=source && source.subscription_id?'/api/source?subscription_id='+encodeURIComponent(source.subscription_id):'/api/subscription?name='+encodeURIComponent(display==='历史来源未知' || display==='来源未知'?'':display);
    const d = await getJSON(url+'&days='+subsDays);
    if(subsSel!==name) return;   // 等待期间用户已改选别的订阅，丢弃过期响应
    subsSeries = {name, pts: Array.isArray(d) ? d : []};
  }catch(e){
    subsSeries = {name, pts: []};
  }
  drawSubsChart();
  renderSubsNodes(name);
}

// 最近一轮该订阅各节点表现：复用 slim 历史（含 provider）+ rowHtml 只读行
async function renderSubsNodes(forName){
  const tbody = document.getElementById('subs-nodes-tbody');
  let hist = histData;
  if(!histLoaded){
    try{ hist = await getJSON('/api/history'); }
    catch(e){ hist = []; }
  }
  if(subsSel!==forName) return;   // 过期响应
  const want = subsRawProvider();
  const source=subsData.find(s=>(s.selection_key||s.provider)===forName);
  const matches=r=>source && source.subscription_id?(r.subscription_ids||[]).includes(source.subscription_id):
    source && source.source_status==='unknown'?!!r.node_id && !(r.subscription_ids||[]).length:
    source && source.source_status==='legacy_unknown'?!r.node_id && (r.provider||'')===(source.provider==='历史来源未知'?'':source.provider):(r.provider||'')===want;
  let rec = null;
  for(let i=hist.length-1;i>=0;i--){
    if((hist[i].results||[]).some(matches)){ rec = hist[i]; break; }
  }
  document.getElementById('subs-chart-sub').textContent =
    rec ? `最近一轮：${rec.ts}` : '';
  const rows = rec ? (rec.results||[]).filter(matches) : [];
  if(!rows.length){ tbody.innerHTML = emptyRow('该订阅暂无节点数据'); return; }
  sortRows(rows, 'score', false);
  tbody.innerHTML = rows.map((r,i)=>rowHtml(r, i, {
    readonly:true, currentNode:'', favs:{has(){return false}},
    expanded:false, selected:false, intelColumns:false,
  })).join('');
}

// 三线趋势：可用率% / 中位速度 Mbps / 平均分。量纲不同，各自按自身最大值归一，
// 图例标注满刻度值（画法与历史视图单节点趋势图同风格）
function drawSubsChart(){
  const cv = document.getElementById('subs-chart');
  if(!cv) return;
  const dpr = window.devicePixelRatio||1;
  const W = cv.clientWidth*dpr, H = cv.clientHeight*dpr;
  if(!W || !H) return;   // 视图隐藏时 clientWidth=0，跳过；切回时 route() 会重画
  cv.width=W; cv.height=H;
  const ctx = cv.getContext('2d');
  ctx.clearRect(0,0,W,H);
  if(!subsSel) return;
  const pts = (subsSeries && subsSeries.name===subsSel) ? subsSeries.pts : [];
  if(!pts.length){
    ctx.fillStyle='#8b949e'; ctx.font=`${12*dpr}px sans-serif`;
    ctx.fillText('该订阅在所选天数内暂无数据', 20*dpr, 30*dpr);
    return;
  }
  const pad=36*dpr, padTop=24*dpr;
  const x=i=> pad + (pts.length===1?(W-2*pad)/2:(W-2*pad)*i/(pts.length-1));
  const yRange=H-pad-padTop;
  ctx.strokeStyle='#30363d'; ctx.font=`${10*dpr}px sans-serif`;
  for(let g=0; g<=4; g++){ const yy=padTop+yRange*g/4;
    ctx.beginPath(); ctx.moveTo(pad,yy); ctx.lineTo(W-pad,yy); ctx.stroke(); }
  const lines = [
    {label:'可用率',   color:'#3fb950', val:p=>p.online_ratio==null?null:p.online_ratio*100, fmt:v=>v.toFixed(0)+'%'},
    {label:'中位速度', color:'#58a6ff', val:p=>p.median_mbps,                              fmt:v=>v.toFixed(1)+'M'},
    {label:'平均分',   color:'#d29922', val:p=>p.avg_score,                                fmt:v=>v.toFixed(1)},
  ];
  let lx = pad;
  for(const ln of lines){
    const vals = pts.map(p=>ln.val(p));
    const present = vals.filter(v=>v!=null);
    if(!present.length) continue;
    const maxV = Math.max(...present)*1.15 || 1;
    const y=v=> padTop + yRange*(1-v/maxV);
    ctx.strokeStyle=ln.color; ctx.lineWidth=2*dpr; ctx.beginPath();
    let started=false;
    vals.forEach((v,i)=>{
      if(v==null) return;   // 缺失点跳过（折线跨过），不产生假零值
      if(started) ctx.lineTo(x(i),y(v)); else { ctx.moveTo(x(i),y(v)); started=true; }
    });
    ctx.stroke();
    ctx.fillStyle=ln.color;
    vals.forEach((v,i)=>{ if(v==null) return;
      ctx.beginPath(); ctx.arc(x(i),y(v),2.5*dpr,0,7); ctx.fill(); });
    const legend = `${ln.label}·满格${ln.fmt(Math.max(...present))}`;
    ctx.fillText(legend, lx, 12*dpr);
    lx += (ctx.measureText ? ctx.measureText(legend).width : legend.length*10*dpr) + 18*dpr;
  }
  ctx.fillStyle='#8b949e';
  pts.forEach((p,i)=>{ if(pts.length<=12||i%2===0)
    ctx.fillText((p.ts||'').slice(5,16), x(i)-20*dpr, H-10*dpr); });
}

/* ==================== 数据加载 ==================== */
async function loadLatest(){
  let rec = null;
  try{ rec = await getJSON('/api/latest'); }
  catch(e){
    latestData = latestData || {};
    document.getElementById('latest-meta').textContent = '读取测速结果失败';
    renderTable(); renderBoard();
    toast('读取测速结果失败', false);
    return;
  }
  if(activeTask && typeof SBTasks!=='undefined' && !SBTasks.terminal(activeTask.status)) return;
  if(rec && rec.results){
    latestData = rec;
    document.getElementById('latest-meta').textContent =
      `上次测速：${rec.ts} · ${rec.results.length} 个节点 · ${rec.mb}MB×${rec.rounds}轮`;
  }else{
    latestData = {};
    document.getElementById('latest-meta').textContent = '暂无测速记录';
  }
  renderTable(); renderBoard();
}

async function loadCurrent(){
  currentGroup='';currentNode='';
  try{
    const r = await getJSON('/api/current');
    if(r.ok){ currentGroup=r.group; currentNode=r.now; }
    const status=document.getElementById('connection-status');
    if(status) status.textContent=r.ok?'Clash Verge 已连接 · 独立 worker':'Clash Verge 未连接 · 请启动 Verge 并开启外部控制器';
  }catch(e){}
  renderMeta();
}

/* ==================== 测速控制 ==================== */
function setRunUi(running){
  if(rootControls) rootControls.setBusy(running);
  document.getElementById('btn-run').disabled = running;
  document.getElementById('btn-cancel').style.display = running ? '' : 'none';
  document.getElementById('prog-wrap').style.display = running ? 'flex' : 'none';
  if(running){
    document.getElementById('log-toggle').style.display = '';
    if(typeof SBTasks==='undefined'){
      document.getElementById('log').style.display = 'block';
      document.getElementById('log-arrow').textContent = '▾';
    }
  }
}

// 启动状态轮询（全局单例）：startRun 与 boot 接管共用；已在轮询时不重复起定时器
function startPolling(){
  if(pollTimer) return;
  pollTimer = setInterval(pollStatus, 1200);
  pollStatus();
}

// boot 接管：测速可能由菜单栏（SwiftBar）触发、或页面刷新前已开始；
// 在跑则恢复运行态 UI 并续上轮询，首次 pollStatus 会把已有 lines 填进日志区
async function resumeRun(){
  let s;
  try{ s = await getJSON('/api/run/status'); }catch(e){ return; }
  if(!s || !s.running) return;
  if(typeof SBTasks!=='undefined' && s.job_id){ await attachTask(s.job_id); return; }
  setRunUi(true);
  startPolling();
}

async function startRun(){
  if(typeof SBTasks!=='undefined'){ await startTask(); return; }
  const body = {
    include: document.getElementById('f-include').value,
    mb: +document.getElementById('f-mb').value,
    rounds: +document.getElementById('f-rounds').value,
    auto_switch: document.getElementById('f-autoswitch').checked,
  };
  const source = document.getElementById('f-source');
  if(source && source.value) body.subscription_ids = [source.value];
  let r;
  try{ r = await post('/api/run', body); }
  catch(e){ toast('启动请求失败', false); return; }
  if(!r.ok){ toast(r.msg||'启动失败', false); return; }
  setRunUi(true);
  startPolling();
}

async function pollStatus(){
  let s;
  try{ s = await getJSON('/api/run/status'); }catch(e){ return; }
  const lines = s.lines || [];
  const log = document.getElementById('log');
  log.textContent = lines.join('\n');
  log.scrollTop = log.scrollHeight;
  // 进度解析：通用 [N/M] 计数定进度条；两阶段模式额外认「Phase 1 粗筛 / Phase 2 精测」标签
  let cur=0, total=0, phase='';
  for(const ln of lines){
    const m = ln.match(/\[\s*(\d+)\/(\d+)\]/);
    if(m){ cur=+m[1]; total=+m[2]; }
    const pm = ln.match(/Phase\s*([12])\s*(粗筛|精测)\s*\[\s*(\d+)\/(\d+)\]/);
    if(pm) phase = `Phase ${pm[1]} ${pm[2]} ${+pm[3]}/${+pm[4]}`;
  }
  if(total) document.getElementById('prog').value = cur/total*100;
  // 进度条旁文本：认得出阶段就显示「Phase 2 精测 3/15」，否则退化显示百分比
  document.getElementById('prog-text').textContent =
    phase || (total ? Math.round(cur/total*100)+'%' : '');
  if(!s.running){
    clearInterval(pollTimer); pollTimer = null;
    setRunUi(false);
    if(s.exit_code===0) toast('测速完成');
    else if(s.exit_code!=null && s.exit_code!==0) toast(`测速结束（退出码 ${s.exit_code}）`, false);
    loadLatest(); loadCurrent();
    histLoaded = false;   // 历史缓存失效，下次进历史视图重拉
    if(currentView()==='history') loadHistory();
    subsLoaded = false;   // 订阅汇总同样失效
    if(currentView()==='subs') loadSubs();
  }
}

function cancelRun(){
  if(activeTask && !SBTasks.terminal(activeTask.status)){
    confirmModal('取消当前任务？已完成结果会保留，清理结束后才显示任务终态。',async()=>{
      try{ const r=await post('/api/jobs/'+activeTask.job_id+'/cancel'); toast(r.msg||'已请求取消，等待清理',!!r.ok); }
      catch(e){ toast('取消请求失败；请刷新任务中心检查状态',false); }
    });
    return;
  }
  confirmModal('中断当前测速？会向测速进程发送中断信号，恢复 Clash 配置后停止。', async ()=>{
    let r;
    try{ r = await post('/api/run/cancel'); }
    catch(e){ toast('中断请求失败', false); return; }
    if(!r.ok){ toast(r.msg||'中断失败', false); return; }
    toast(r.msg||'已中断测速');
    pollStatus();
  });
}

/* ==================== 节点操作 ==================== */
// 切换一律先取后端新鲜预览：目标运行时名、稳定 node_id、来源、实际策略组及其
// 当前选择、配置根 revision。确认时把整份计划回传后端做逐字段复核；任何变化
// 都要求刷新后重新确认，且不写控制器。取消模态框不触发任何切换。
function switchConfirmText(plan){
  const labels=(plan.subscriptions||[]).map(s=>s.name||'名称未知').join('、');
  const source = plan.source_status==='verified'?(plan.subscription_name||labels||'订阅名称未知'):
    plan.source_status==='ambiguous'?`多个来源（无法唯一确认）${labels?'：'+labels:''}`:'来源未知';
  const identity = plan.identity_strength==='weak' ? '（名称范围，身份未验证）' : '';
  return `切换到 ${plan.runtime_name}？涉及策略组：${plan.group}（当前：${plan.current||'尚未确认'}）。来源：${source}${identity}。目录、来源或选择变化时会要求刷新后重新确认，不会直接切换。`;
}
async function switchNode(name, btn){
  if(btn?.disabled) return;
  const nodeId = (btn && btn.dataset && btn.dataset.nodeId) ? btn.dataset.nodeId : '';
  const body = nodeId ? {node_id:nodeId} : {name};
  let preview, requestFailed = false;
  if(btn && 'disabled' in btn) btn.disabled = true;
  try{ preview = await post('/api/switch/preview', body); }
  catch(e){ requestFailed = true; }
  finally{
    // 预览只读；无论成功与否都在打开模态框前恢复按钮，取消时不会永久禁用。
    if(btn && 'disabled' in btn) btn.disabled = false;
  }
  if(requestFailed){
    toast('无法读取切换信息，请检查本地后端连接', false);
    return;
  }
  if(!preview || !preview.ok || !preview.plan){
    toast((preview && preview.msg) || '无法确认切换信息，请刷新目录后重试', false);
    return;
  }
  const plan = preview.plan;
  confirmModal(switchConfirmText(plan), async ()=>{
    if(btn && 'disabled' in btn){ btn.disabled = true; btn.textContent = '切换中…'; }
    try{
      const r = await post('/api/switch', {node_id:plan.node_id, confirmation:plan});
      if(r.ok){currentNode=r.now||plan.runtime_name;if(r.group)currentGroup=r.group;
        toast(r.msg||`已切换 → ${plan.runtime_name}`);renderMeta();}
      else toast(r.msg||'切换失败，请刷新目录后重新确认',false);
    }catch(e){toast('切换请求失败；请刷新当前节点后检查结果',false);}
    finally{
      if(btn){btn.disabled=false;btn.textContent='切换';}renderTable();
      if(btn){
        const row=[...document.querySelectorAll('tr[data-node-id]')].find(el=>
          el.dataset.nodeId===plan.node_id || (!el.dataset.nodeId && el.dataset.name===plan.runtime_name));
        if(row?.focus) row.focus({preventScroll:true});
      }
    }
  },btn||document.activeElement);
}

// 「查看 30 天趋势」：跳到历史视图并选中该节点（含该节点的最近一轮）
function gotoTrend(name,nodeId=''){
  setHash('#/history');   // 触发 hashchange → route()
  const go = ()=>{
    for(let i=histData.length-1;i>=0;i--){
      if((histData[i].results||[]).some(x=>nodeId?x.node_id===nodeId:x.name===name)){ histSelRun=i; break; }
    }
    histSelNode = name;
    histSelNodeId=nodeId;
    renderHistList(); renderHistTable(); fetchNodeTrend(name,nodeId);
  };
  if(histLoaded) go();
  else loadHistory().then(go);
}

function quitPanel(){
  confirmModal('停止 SpeedBench 面板？若测速仍在进行，会先中断测速并恢复 Clash 配置，然后停止面板。', async ()=>{
    try{ await post('/api/quit'); }catch(e){}
    document.body.innerHTML =
      '<div style="text-align:center;padding:80px;color:#8b949e">面板已停止，可以关闭此标签页。<br>' +
      '下次双击 Clash SpeedBench 图标重新启动。</div>';
  });
}

/* ==================== IP Intelligence / Leak Audit ==================== */
// Credentials deliberately have no localStorage representation.  These
// variables contain only the latest leak candidate result in page memory.
let lastLeakPayload = null;
let lastLeakEvaluation = null;

function providerStatusLabel(name, item){
  const labels = {'ip-api':'ip-api', ipinfo:'IPinfo', ipqs:'IPQS', scamalytics:'Scamalytics'};
  const text = item && item.status ? item.status : 'unknown';
  const configured = item && item.configured ? '✓' : '未配置';
  const cache = item && item.cache ? ` · ${esc(item.cache)}` : '';
  return `<div class="provider-status"><b>${esc(labels[name]||name)}</b><span>${esc(configured)} · ${esc(text)}${cache}</span></div>`;
}

async function loadProviderStatus(){
  const box = document.getElementById('provider-status');
  if(!box) return;
  try{
    const data = await getJSON('/api/ip-intel/status');
    const providers = data && data.providers || {};
    const names = ['ip-api','ipinfo','ipqs','scamalytics'];
    box.innerHTML = names.map(n=>providerStatusLabel(n, providers[n]||{})).join('');
  }catch(e){ box.textContent = 'Provider 状态暂时不可用'; }
}

function setLeakStatus(evaluation){
  const box = document.getElementById('leak-status');
  if(!box) return;
  const state = evaluation && evaluation.status || 'unknown';
  box.className = `leak-status ${esc(state)}`;
  box.textContent = evaluation && evaluation.status_text || '无法确认';
  const summary = document.getElementById('leak-summary');
  if(summary){
    const n = evaluation && evaluation.candidates ? evaluation.candidates.length : 0;
    summary.textContent = `已收到 ${n} 个 ICE candidate。${evaluation && evaluation.notes && evaluation.notes.length ? evaluation.notes.join('；') : '结果为当前浏览器环境的 best-effort 判断。'}`;
  }
}

function renderLeakDetails(evaluation){
  const box = document.getElementById('leak-details');
  if(!box) return;
  const candidates = (evaluation && evaluation.candidates)||[];
  const warnings = (evaluation && evaluation.warnings)||[];
  const notes = (evaluation && evaluation.notes)||[];
  const rows = candidates.map(c=>`<tr><td>${esc(c.type||'-')}</td><td class="mono">${esc(c.address||'-')}</td><td>${esc(c.protocol||'-')}</td></tr>`).join('');
  const warningHtml = warnings.length ? `<div class="leak-warning"><b>⚠ 需要注意</b><ul>${warnings.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>` : '';
  const noteHtml = notes.length ? `<div class="card-sub">${notes.map(x=>esc(x)).join('；')}</div>` : '';
  box.innerHTML = `${warningHtml}${noteHtml}<div class="table-wrap"><table class="compact"><thead><tr><th>类型</th><th>地址</th><th>协议</th></tr></thead><tbody>${rows||'<tr><td colspan="3">没有可显示的 candidate</td></tr>'}</tbody></table></div>`;
}

async function browserExitIp(url){
  try{
    const response = await fetch(url, {cache:'no-store'});
    const data = await response.json();
    const ip = data && data.ip;
    return typeof ip === 'string' ? ip : null;
  }catch(e){ return null; }
}

function collectWebRTCCandidates(){
  if(typeof RTCPeerConnection !== 'function')
    return Promise.resolve({candidates:[], collection_complete:false, policy_blocked:true});
  return new Promise(resolve=>{
    const candidates = [];
    let pc = null, done = false;
    const finish = (error, blocked)=>{
      if(done) return;
      done = true;
      try{ if(pc) pc.close(); }catch(e){}
      resolve({candidates, collection_complete:!error && !blocked,
               collection_error:error||null, policy_blocked:!!blocked});
    };
    try{
      pc = new RTCPeerConnection({iceServers:[{urls:'stun:stun.l.google.com:19302'}]});
      pc.onicecandidate = event=>{
        if(!event || !event.candidate) return;
        let item = event.candidate;
        try{ if(typeof item.toJSON === 'function') item = item.toJSON(); }
        catch(e){}
        // Keep only the standard browser fields/a-line.  No credentials or
        // third-party provider data are sent with the audit request.
        candidates.push({type:item.type, address:item.address, protocol:item.protocol,
                         port:item.port, candidate:item.candidate});
      };
      pc.onicegatheringstatechange = ()=>{
        if(pc.iceGatheringState === 'complete') finish(null, false);
      };
      pc.createDataChannel('speedbench-leak');
      pc.createOffer().then(offer=>pc.setLocalDescription(offer))
        .catch(()=>finish('stun_failed', false));
      setTimeout(()=>finish('stun_timeout', false), 7000);
    }catch(e){ finish('webrtc_unavailable', true); }
  });
}

async function runLeakAudit(){
  const run = document.getElementById('btn-leak-run');
  const restoreFocus = document.activeElement===run;
  if(run){ run.disabled = true; run.textContent = '检测中…'; }
  setLeakStatus({status:'unknown', status_text:'正在采集 WebRTC candidate…'});
  try{
    const [exit_ipv4, exit_ipv6, gathered] = await Promise.all([
      browserExitIp('https://api.ipify.org?format=json'),
      browserExitIp('https://api6.ipify.org?format=json'),
      collectWebRTCCandidates(),
    ]);
    const payload = Object.assign({}, gathered, {exit_ipv4, exit_ipv6,
      client_environment: window.SPEEDBENCH_ENV?.client==='webview'?'webview':'browser'});
    lastLeakPayload = payload;
    const evaluation = await post('/api/leak/evaluate', payload);
    lastLeakEvaluation = evaluation;
    setLeakStatus(evaluation); renderLeakDetails(evaluation);
    const save = document.getElementById('btn-leak-save');
    if(save) save.disabled = !evaluation || !!evaluation.msg;
  }catch(e){
    lastLeakPayload = null; lastLeakEvaluation = null;
    const evaluation = {status:'unknown', status_text:'无法确认', notes:['浏览器出口或本地 API 不可用']};
    setLeakStatus(evaluation); renderLeakDetails(evaluation);
  }finally{
    if(run){ run.disabled = false; run.textContent = '开始 WebRTC 检测'; }
    if(restoreFocus && document.activeElement===document.body && run?.focus) run.focus({preventScroll:true});
  }
}

async function saveLeakAudit(){
  if(!lastLeakPayload) return;
  const payload = Object.assign({}, lastLeakPayload);
  payload.dns_status = (document.getElementById('dns-status')||{}).value || 'unknown';
  try{
    const result = await post('/api/leak/audit', payload);
    if(result && result.persistence && result.persistence.saved) toast('泄漏审计已保存');
    else toast('审计结果已完成，但历史库暂不可用', false);
    loadLeakHistory();
  }catch(e){ toast('保存审计失败', false); }
}

function renderLeakHistory(data){
  const box = document.getElementById('leak-history');
  if(!box) return;
  const rows = data && data.audits || [];
  if(!rows.length){ box.textContent = data && data.available===false ? '历史库尚未提供 leak_audits 接口' : '尚无本地保存记录'; return; }
  box.innerHTML = rows.map(x=>`<div class="history-chip"><b>${esc(x.created_at||x.ts||'-')}</b> · ${esc(x.webrtc_status||'unknown')} · DNS ${esc(x.dns_status||'unknown')} · ${x.details?.client_environment==='webview'?'系统 WebView':x.details?.client_environment==='browser'?'浏览器':'旧记录：环境未标记'}</div>`).join('');
}

async function loadLeakHistory(){
  try{ renderLeakHistory(await getJSON('/api/leak/audits?limit=20')); }
  catch(e){ renderLeakHistory({audits:[]}); }
}

async function saveIpIntelSettings(){
  const body = {
    ipinfo_token: (document.getElementById('setting-ipinfo-token')||{}).value || '',
    ipqs_key: (document.getElementById('setting-ipqs-key')||{}).value || '',
    scamalytics_username: (document.getElementById('setting-scamalytics-username')||{}).value || '',
    scamalytics_key: (document.getElementById('setting-scamalytics-key')||{}).value || '',
    scamalytics_region: (document.getElementById('setting-scamalytics-region')||{}).value || '',
  };
  try{
    const result = await post('/api/ip-intel/settings', body);
    const msg = document.getElementById('settings-msg');
    if(msg) msg.textContent = result.msg || (result.ok ? '已更新' : '更新失败');
    if(result.ok) loadProviderStatus();
  }catch(e){ const msg=document.getElementById('settings-msg'); if(msg) msg.textContent='设置请求失败'; }
}

function clearIpIntelSettings(){
  for(const id of ['setting-ipinfo-token','setting-ipqs-key','setting-scamalytics-username','setting-scamalytics-key']){
    const el=document.getElementById(id); if(el) el.value='';
  }
  const region=document.getElementById('setting-scamalytics-region'); if(region) region.value='';
  saveIpIntelSettings();
}

async function desktopAction(action){
  try{
    const request=await post('/api/desktop/actions',{action});
    if(!request.ok){toast(request.msg||'桌面操作不可用',false);return;}
    for(let i=0;i<12;i++){
      await new Promise(resolve=>setTimeout(resolve,500));
      const response=await fetch('/api/desktop/actions/'+encodeURIComponent(request.request_id),{headers:{'X-SpeedBench-Token':SB_TOKEN}});
      const result=await response.json();
      if(result.status==='opened'){toast('已请求系统浏览器打开；请人工查看结果');return;}
      if(['failed','expired','unavailable'].includes(result.status)){toast('系统浏览器打开失败；请手动访问官方测试站点',false);return;}
    }
    toast('打开请求尚未确认；不要将它视为已完成检测',false);
  }catch(e){toast('桌面操作连接失败',false);}
}
function openDnsAudit(url){
  const action={'https://browserleaks.com/dns':'browserleaks_dns','https://www.dnsleaktest.com/':'dnsleaktest'}[url];
  if(!action) return; // Never allow subscription/node text to choose a URL.
  if(SB_DESKTOP){desktopAction(action);return;}
  // noopener/noreferrer is explicit; the target pages are never scraped.
  try{ const child=window.open(url, '_blank', 'noopener,noreferrer'); if(child) child.opener=null; }
  catch(e){}
}

/* ==================== hash 路由 ==================== */
const VIEWS = ['nodes','tasks','history','subs','leak','settings','about'];
function currentView(){
  let h = '';
  try{ h = (window.location && window.location.hash) || ''; }catch(e){ h=''; }
  const v = h.replace(/^#\/?/, '');
  return VIEWS.includes(v) ? v : 'nodes';
}
function setHash(h){ try{ window.location.hash = h; }catch(e){} }

function route(){
  const v = currentView();
  for(const x of VIEWS){
    const el = document.getElementById('view-'+x);
    if(el) el.style.display = x===v ? '' : 'none';
  }
  const navs = document.querySelectorAll('.nav-item');
  for(const a of navs){
    const on = !!(a.dataset && a.dataset.view===v);
    if(a.classList) a.classList.toggle('on', on);
    if(typeof a.setAttribute==='function'){
      if(on) a.setAttribute('aria-current','page');
      else if(typeof a.removeAttribute==='function') a.removeAttribute('aria-current');
    }
  }
  if(v==='history'){
    if(!histLoaded) loadHistory(); else drawChart();   // 切回时 canvas 已有宽度，重画
  }
  if(v==='subs'){
    if(!subsLoaded) loadSubs(); else if(subsSel) drawSubsChart();
  }
  if(v==='leak') loadLeakHistory();
  if(v==='settings') loadProviderStatus();
  if(v==='tasks') loadTasks();
}

/* ==================== 事件绑定（全部 addEventListener/委托） ==================== */
function init(){
  initTaskControls();
  const sourceRefresh = document.getElementById('btn-source-refresh');
  if(sourceRefresh) sourceRefresh.addEventListener('click',loadSourceCatalog);
  // 节点表格：事件委托；节点名一律走 dataset（HTML 属性经 esc 转义），绝不拼接进 JS 源码
  document.getElementById('tbody').addEventListener('click', e=>{
    const fv = e.target.closest('.fav');
    if(fv && fv.dataset.name!=null){
      if(fv.dataset.nodeId){
        const id=fv.dataset.nodeId; if(favIds.has(id)) favIds.delete(id); else favIds.add(id);
        lsSet('sb_favs_v2',JSON.stringify([...favIds])); renderTable();renderBoard();updateTaskBudget();
      }else toggleFav(fv.dataset.name);
      return;
    }
    const sw = e.target.closest('button.sw');
    if(sw && sw.dataset.name!=null){ switchNode(sw.dataset.name, sw); return; }
    const tb = e.target.closest('button.trend');
    if(tb && tb.dataset.name!=null){ gotoTrend(tb.dataset.name,tb.dataset.nodeId||''); return; }
    const cell = e.target.closest('td.stars');
    if(cell && cell.dataset.name!=null){ gotoTrend(cell.dataset.name,cell.closest('tr').dataset.nodeId||''); return; }
    const tr = e.target.closest('tr[data-name]');
    if(tr && tr.dataset.name!=null){   // 点击行：展开/收起详情面板
      const key=tr.dataset.rowKey||tr.dataset.name;
      expandedNode = (expandedNode===key) ? null : key;
      renderTable();
    }
  });
  // 历史表格（只读）：点击行选中节点看趋势
  document.getElementById('hist-tbody').addEventListener('click', e=>{
    const tr = e.target.closest('tr[data-name]');
    if(tr && tr.dataset.name!=null){
      histSelNode = tr.dataset.name;
      histSelNodeId=tr.dataset.nodeId||'';
      renderHistTable();
      fetchNodeTrend(histSelNode);
    }
  });
  // 历史轮次列表：点击选中该轮（默认选中冠军节点）
  document.getElementById('hist-list').addEventListener('click', e=>{
    const it = e.target.closest('.hist-item');
    if(it && it.dataset.i!=null){
      histSelRun = +it.dataset.i;
      const rec = histData[histSelRun];
      const ch = rec ? championOf(rec) : null;
      const selected=ch || ((rec&&rec.results)||[])[0];
      histSelNode = selected?selected.name:null;
      histSelNodeId=selected?selected.node_id||'':'';
      renderHistList(); renderHistTable();
      restoreListFocus('hist-list','.hist-item','i',histSelRun);
      if(histSelNode) fetchNodeTrend(histSelNode); else drawChart();
    }
  });
  // 排序表头：节点视图 / 历史视图各自独立
  const ths = document.querySelectorAll('th.sort');
  for(const th of ths) th.addEventListener('click', ()=>setSort(th.dataset.k));
  const hths = document.querySelectorAll('th.hsort');
  for(const th of hths) th.addEventListener('click', ()=>setHistSort(th.dataset.k));
  // Profile 按钮：初始化选中态（localStorage 恢复）+ 点击切换
  const pfs = document.querySelectorAll('#profile-bar .pf');
  for(const b of pfs){
    if(b.classList) b.classList.toggle('on', b.dataset.p===currentProfile);
    b.addEventListener('click', ()=>setProfile(b.dataset.p));
  }
  // 地区榜：标题点击折叠/展开（默认折叠），条目点击看该节点趋势
  document.getElementById('board-toggle').addEventListener('click', e=>{
    const body = document.getElementById('board-body');
    const open = body.style.display==='none';
    body.style.display = open?'':'none';
    document.getElementById('board-arrow').textContent = open?'▾':'▸';
    if(e && e.currentTarget && typeof e.currentTarget.setAttribute==='function')
      e.currentTarget.setAttribute('aria-expanded',String(open));
  });
  document.getElementById('board-body').addEventListener('click', e=>{
    const it = e.target.closest('.board-item');
    if(it && it.dataset.name!=null) gotoTrend(it.dataset.name,it.dataset.nodeId||'');
  });
  // 运行日志折叠
  document.getElementById('log-toggle').addEventListener('click', e=>{
    const log = document.getElementById('log');
    const open = log.style.display==='none';
    log.style.display = open?'block':'none';
    document.getElementById('log-arrow').textContent = open?'▾':'▸';
    if(e && e.currentTarget && e.currentTarget.setAttribute) e.currentTarget.setAttribute('aria-expanded',String(open));
    if(open && typeof SBTasks!=='undefined') getJSON('/api/run/status').then(s=>{log.textContent=(s.lines||[]).join('\n');}).catch(()=>{});
  });
  // 搜索框：按节点名/订阅名实时过滤
  document.getElementById('f-search').addEventListener('input', e=>{
    searchText = (e.target && e.target.value) || '';
    renderTable();
  });
  // 订阅视图：天数切换重拉汇总；点汇总行进单订阅详情；节点行点评分看单节点趋势
  const subsDaysSel = document.getElementById('subs-days');
  if(subsDaysSel) subsDaysSel.value = String(subsDays);
  subsDaysSel.addEventListener('change', e=>{
    subsDays = +((e.target && e.target.value) || 30) || 30;
    lsSet('sb_subs_days', String(subsDays));
    loadSubs();
  });
  document.getElementById('subs-tbody').addEventListener('click', e=>{
    const tr = e.target.closest('tr[data-provider]');
    if(tr && tr.dataset.provider!=null){
      selectSub(tr.dataset.provider);
      restoreListFocus('subs-tbody','.subs-pick','provider',tr.dataset.provider);
    }
  });
  document.getElementById('btn-subs-catalog-refresh')?.addEventListener('click',loadSourceCatalog);
  document.getElementById('subs-catalog-tbody')?.addEventListener('click',e=>{
    const button=e.target.closest('button[data-test-source]');
    if(!button)return;
    const id=button.dataset.testSource;
    if(sourceCatalog.status!=='ok' || !(sourceCatalog.sources||[]).some(s=>s.loaded && s.subscription_id===id)){
      toast('订阅已失效或未加载，请刷新目录',false);return;
    }
    const select=document.getElementById('f-source');select.value=id;
    renderNodePicker();updateTaskBudget();window.location.hash='#/nodes';route();select.focus?.({preventScroll:true});
  });
  document.getElementById('subs-nodes-tbody').addEventListener('click', e=>{
    const cell = e.target.closest('td.stars');
    if(cell && cell.dataset.name!=null) gotoTrend(cell.dataset.name,cell.closest('tr').dataset.nodeId||'');
  });
  document.getElementById('btn-run').addEventListener('click', startRun);
  document.getElementById('btn-cancel').addEventListener('click', cancelRun);
  const leakRun = document.getElementById('btn-leak-run');
  if(leakRun) leakRun.addEventListener('click', runLeakAudit);
  const leakSave = document.getElementById('btn-leak-save');
  if(leakSave) leakSave.addEventListener('click', saveLeakAudit);
  const settingsSave = document.getElementById('btn-settings-save');
  if(settingsSave) settingsSave.addEventListener('click', saveIpIntelSettings);
  const settingsClear = document.getElementById('btn-settings-clear');
  if(settingsClear) settingsClear.addEventListener('click', clearIpIntelSettings);
  for(const button of document.querySelectorAll('.dns-open'))
    button.addEventListener('click', ()=>openDnsAudit(button.dataset.dnsUrl));
  // 退出按钮两处：导航底部 + 「关于」视图，复用同一 quitPanel
  document.getElementById('btn-quit').addEventListener('click', quitPanel);
  document.getElementById('btn-quit-2').addEventListener('click', quitPanel);
  // 确认对话框
  document.getElementById('modal-yes').addEventListener('click', ()=>{
    const f = modalYes; closeModal(); if(f) f();
  });
  document.getElementById('modal-no').addEventListener('click', closeModal);
  document.getElementById('modal-mask').addEventListener('click', e=>{
    if(e.target===e.currentTarget) closeModal();
  });
  document.getElementById('modal-mask').addEventListener('keydown',e=>{
    if(e.key==='Escape'){e.preventDefault();closeModal();}
    if(e.key==='Tab'){
      const no=document.getElementById('modal-no'), yes=document.getElementById('modal-yes');
      if((e.shiftKey && document.activeElement===no)||(!e.shiftKey && document.activeElement===yes)){
        e.preventDefault();(e.shiftKey?yes:no).focus();
      }
    }
  });
  window.addEventListener('hashchange', route);
  window.addEventListener('resize', ()=>{
    if(currentView()==='history') drawChart();
    if(currentView()==='subs') drawSubsChart();
  });
}

/* ==================== 共享任务界面（传输状态在 tasks.js） ==================== */
function scopedNodes(){
  const source=document.getElementById('f-source').value;
  return (sourceCatalog.nodes||[]).filter(n=>
    source==='__favorites__'?favIds.has(n.node_id):
    source==='__manual__'?selectedNodeIds.has(n.node_id):
    !source || (n.subscription_ids||[]).includes(source));
}
function renderNodePicker(){
  const panel=document.getElementById('manual-scope'),picker=document.getElementById('node-picker');
  if(!panel || !picker || typeof SBTasks==='undefined') return;
  panel.hidden=document.getElementById('f-source').value!=='__manual__';
  const q=(document.getElementById('f-node-search').value||'').toLowerCase();
  picker.innerHTML=(sourceCatalog.nodes||[]).filter(n=>`${n.runtime_name} ${sourceLabel(n)}`.toLowerCase().includes(q)).map(n=>
    `<label class="node-choice"><input type="checkbox" data-node-id="${esc(n.node_id||'')}"${selectedNodeIds.has(n.node_id)?' checked':''}${n.identity_strength==='strong'?'':' disabled'}> <span>${esc(n.runtime_name)}<small>${esc(sourceLabel(n))}</small></span></label>`).join('')||'<p class="muted">暂无可选节点。请刷新订阅目录。</p>';
}
function updateTaskBudget(){
  const el=document.getElementById('task-budget');
  if(!el || !taskConfig || typeof SBTasks==='undefined') return;
  const mode=document.getElementById('f-mode').value||'standard';
  const config=Object.assign({},(taskConfig.modes||{})[mode]);
  if(mode==='legacy') config.measure_all=true;
  const mb=document.getElementById('f-mb').value;
  if(mb!=='') config.mb=Number(mb);
  config.rounds=Number(document.getElementById('f-rounds').value)||1;
  config.multi=document.getElementById('f-multi').checked;
  const count=scopedNodes().length;
  const budget=SBTasks.budget(config,count);
  el.textContent=`范围 ${count} 节点 · ${config.bandwidth?'精测最多 '+(config.measure_all?count:Math.min(count,config.top_n))+' 节点':'不请求带宽'} · 带宽样本预算上限 ${budget} MB（不含小流量探测；不是实际流量）`;
}
async function startTask(){
  if(sourceCatalog.status==='ok' && !(sourceCatalog.nodes||[]).length){
    toast('当前没有已加载节点。请先在 Verge 加载订阅，再刷新订阅。',false);return;
  }
  const mode=document.getElementById('f-mode').value||'standard';
  const body={mode,target_profile:document.getElementById('f-target').value||'daily',
    rounds:Number(document.getElementById('f-rounds').value)||1,
    auto_switch:document.getElementById('f-autoswitch').checked,
    multi:document.getElementById('f-multi').checked,
    all_ip:document.getElementById('f-all-ip').checked};
  const mb=document.getElementById('f-mb').value;
  if(mb!=='') body.mb=Number(mb);
  const include=document.getElementById('f-include').value;
  if(include) body.include=include;
  const source=document.getElementById('f-source').value;
  if(source==='__manual__' || source==='__favorites__'){
    body.node_ids=[...new Set(scopedNodes().filter(n=>n.identity_strength==='strong').map(n=>n.node_id))];
    if(!body.node_ids.length){ toast('所选范围没有可核验节点。请刷新订阅或重新选择。',false); return; }
  }else if(source) body.subscription_ids=[source];
  if(mode==='ip' && body.multi){ toast('IP 专项不请求带宽，请关闭“4 路峰值”。',false); return; }
  if(mode==='legacy'){
    body.workers=1;
    const nodes=scopedNodes();
    if(!nodes.length || nodes.some(n=>n.identity_strength!=='strong' ||
        !/^node_v2_[0-9a-f]{32}$/.test(n.node_id||''))){
      toast('串行范围无法固定到可核验节点。请刷新目录或手动选择身份明确的节点。',false);return;
    }
    // Freeze the confirmed membership. CLI revalidates these identities against
    // its fresh catalog; new subscription members cannot expand this task.
    body.node_ids=[...new Set(nodes.map(n=>n.node_id))];
    const count=body.node_ids.length;
    const scope=document.getElementById('f-source').selectedOptions?.[0]?.textContent||'所选来源';
    await loadCurrent();
    const text=`改用兼容串行？将对 ${scope} 的最多 ${count} 个已选节点逐个全测${include?'（名称过滤仍生效）':''}，期间临时切 GLOBAL 并调整沿途策略组，会影响当前活动连接。当前：${currentGroup||'尚未确认策略组'} = ${currentNode||'尚未确认节点'}。确认范围已固定，目录变化时会重新核验；结束或取消后尝试恢复原模式和选择，清理失败会明确报告。${body.auto_switch?'另允许结束后按本次使用目标切换到已测范围内冠军。':''}`;
    confirmModal(text,()=>{body.allow_serial=true;dispatchTask(body);});
  }else if(body.auto_switch){
    confirmModal('测速完成后按本次使用目标自动切换当前策略组到已测范围内冠军？此操作会改变当前活动节点。',()=>dispatchTask(body));
  }else await dispatchTask(body);
}
async function dispatchTask(body){
  setRunUi(true);
  try{
    const r=await post('/api/jobs',body);
    if(!r.ok){ setRunUi(false);toast(r.msg||'无法启动，请检查 Verge 连接和所选范围',false);return; }
    setProfile(({balanced:'all',daily:'daily',download:'download',ip:'ipclean',residential:'residential'})[body.target_profile]||'all');
    await attachTask(r.job_id);
  }catch(e){setRunUi(false);toast('启动或任务连接失败；请刷新任务中心，勿重复启动',false);}
}
async function attachTask(id){
  if(!taskClient) taskClient=new SBTasks.Client({get:getJSON,EventSource:window.EventSource,
    onChange:showTask,onError:e=>toast('任务连接中断；刷新任务中心可恢复',false)});
  await taskClient.attach(id);
}
function powerNotice(metrics){
  // Fixed Chinese explanation derived only from known interruption counters.
  // Never render raw API errors, caller text, timestamps or durations.
  let resumes=0,clockErrors=0;
  for(const metric of Object.values(metrics||{})){
    const counters=(metric&&metric.counters)||{};
    if(Number.isSafeInteger(counters.system_resumes)) resumes+=counters.system_resumes;
    if(Number.isSafeInteger(counters.power_clock_errors)) clockErrors+=counters.power_clock_errors;
  }
  const parts=[];
  if(resumes>0) parts.push('检测到系统从睡眠/休眠中恢复，任务已中断并保留已测部分结果；未报告的流量不计入统计。可手动重新开始测速，不会自动重试或自动恢复测量。');
  if(clockErrors>0) parts.push('本机挂起/恢复时钟不可用，任务已中断并保留已测部分结果；请重启应用后再试，不会自动重试。');
  return parts.join(' ');
}
function showTask(task){
  activeTask=task;
  const running=!SBTasks.terminal(task.status);
  setRunUi(running);
  document.getElementById('task-card').style.display='';
  // Structured tasks keep logs collapsed; no stdout regex determines state.
  const log=document.getElementById('log');
  if(log && document.getElementById('log-toggle').getAttribute && document.getElementById('log-toggle').getAttribute('aria-expanded')!=='true') log.style.display='none';
  const progress=task.progress;
  const text=taskLabels[task.status]||'状态未知';
  document.getElementById('prog-text').textContent=text+(progress?` ${progress.completed}/${progress.total}`:'');
  document.getElementById('prog').value=progress&&progress.total?100*progress.completed/progress.total:0;
  const bytes=Object.values(task.metrics||{}).reduce((n,m)=>n+(m.bytes||0),0);
  const elapsed=task.elapsed_ms==null?'未知':(task.elapsed_ms/1000).toFixed(1)+'s';
  document.getElementById('task-stage').textContent=text;
  const notice=powerNotice(task.metrics);
  document.getElementById('task-summary').textContent=`${running?'截至此更新，已等待':'总耗时'} ${elapsed} · 已报告实际下载 ${(bytes/1000000).toFixed(2)} MB · IPv4/IPv6 独立更新${running?' · 剩余时间尚无法可靠估计':' · 中断中的未报告字节不计入此值'}${notice?' · '+notice:''}`;
  latestData={ts:task.started_at,results:task.results||[],task:task.config};
  renderTable();renderBoard();
  if(!running){histLoaded=false;subsLoaded=false;}
}
function renderTaskResultMeta(task){
  const rows=task.results||[],running=!SBTasks.terminal(task.status),text=taskLabels[task.status]||'状态未知';
  const hasRecommendation=rows.some(r=>profileScore(r)!=null);
  document.getElementById('latest-meta').textContent=`${task.config?.mode||task.mode||'未知模式'} · ${text} · ${rows.length} 个已返回节点 · ${hasRecommendation?'已测范围内推荐':running?'等待可用于推荐的观测结果':'当前目标没有可推荐结果；请展开指标状态，检查节点连接或调整测速模式后重试'}`;
}
let taskListRevision=0,taskDetailRevision=0,selectedTaskHistoryId='';
function clearTaskHistory(){
  ++taskDetailRevision;selectedTaskHistoryId='';
  const detail=document.getElementById('task-detail');
  if(detail){detail.hidden=true;detail.innerHTML='';}
}
async function loadTasks(){
  const list=document.getElementById('task-list');
  if(!list || typeof SBTasks==='undefined') return;
  const revision=++taskListRevision;
  try{
    const data=await getJSON('/api/tasks');
    if(revision!==taskListRevision) return;
    if(selectedTaskHistoryId && !(data.tasks||[]).some(t=>t.job_id===selectedTaskHistoryId)) clearTaskHistory();
    list.innerHTML=(data.tasks||[]).map(t=>`<button class="task-history-item mini" data-job-id="${esc(t.job_id)}"><b>${esc(taskLabels[t.status]||t.status)}</b><span>${esc(t.mode)} · ${esc(t.started_at)} · ${t.elapsed_ms==null?'耗时未知':(t.elapsed_ms/1000).toFixed(1)+'s'}</span></button>`).join('')||'<p class="muted">暂无任务。选择订阅与模式，开始一次测速。</p>';
  }catch(e){if(revision===taskListRevision) list.textContent='无法读取任务历史，请检查本地数据目录权限后刷新。';}
}
async function showTaskHistory(id){
  if(!/^job_[0-9a-f]{32}$/.test(id)) return;
  const revision=++taskDetailRevision;
  const task=await getJSON('/api/tasks/'+id), detail=document.getElementById('task-detail');
  if(revision!==taskDetailRevision || historyImportBusy) return;
  if(!task || task.job_id!==id){toast('任务已失效，请刷新',false);return;}
  selectedTaskHistoryId=id;
  detail.hidden=false;
  const metrics=Object.entries(task.metrics||{}).map(([phase,m])=>`<tr><td>${esc(phase)}</td><td>${(m.duration_ms/1000).toFixed(2)}s</td><td>${m.successes}/${m.attempts}</td><td>${(m.bytes/1000000).toFixed(2)} MB</td></tr>`).join('');
  const notice=powerNotice(task.metrics);
  detail.innerHTML=`<h2>${esc(taskLabels[task.status]||task.status)}</h2><p class="muted">${esc(task.mode)} · ${task.partial?'部分结果':'完整任务'} · 重叠阶段耗时不可直接相加。流量仅统计已报告的 curl 实际字节。</p>${notice?`<p class="muted">${esc(notice)}</p>`:''}<div class="table-wrap"><table><thead><tr><th>阶段</th><th>耗时</th><th>成功/尝试</th><th>已报告实际下载</th></tr></thead><tbody>${metrics||emptyRow('旧记录无阶段耗时',4)}</tbody></table></div><div class="table-wrap"><table><thead><tr><th>#</th><th>节点</th><th>延迟</th><th>带宽</th><th>Network</th><th>IP Grade</th><th>IP 类型</th><th>风险</th><th>标签</th><th></th></tr></thead><tbody>${(task.results||[]).map((r,i)=>rowHtml(r,i,{readonly:true,favs:new Set()})).join('')||emptyRow('任务尚未返回节点结果',10)}</tbody></table></div>`;
}
function applyTheme(){
  const theme=lsGet('sb_theme')||'system';
  const dark=theme==='dark'||(theme==='system' && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  if(document.documentElement) document.documentElement.dataset.theme=dark?'dark':'light';
  const input=document.getElementById('f-theme');if(input) input.value=theme;
}
function updatePendingFavorites(){
  const pending=document.getElementById('pending-favorites');if(!pending) return;
  const known=new Set((sourceCatalog.nodes||[]).filter(n=>n.identity_strength==='strong').map(n=>n.node_id));
  const unresolved=[...favIds].filter(id=>!known.has(id)).length;
  pending.textContent=(favs.size?`待确认旧收藏：${[...favs].join('、')}。不会按名称合并到其他节点。 `:'')+
    (unresolved?`${unresolved} 个稳定 ID 收藏尚未匹配当前目录；不会自动改绑。`:
      '新收藏按稳定节点身份保存；未确认项不会用于自动扩大测速范围。');
}
function alternateKinds(alternate){
  if(!alternate) return '';
  return [alternate.jsonl_exists?'JSONL':null,alternate.database_exists?'SQLite':null].filter(Boolean).join(' 与 ');
}
function nodesDataStatusText(info){
  const history=info.history||{};
  const currentKinds=[history.jsonl_exists?'JSONL':null,history.database_exists?'SQLite':null].filter(Boolean);
  const current=currentKinds.length
    ? `本实例数据目录已有历史文件（${currentKinds.join(' 与 ')}）；存在不代表内容有效，仍需核验。`
    : '本实例数据目录尚无历史文件（JSONL 与 SQLite 均无）。';
  let text=`本实例数据目录：${info.data_home}\n${current}`;
  const kinds=alternateKinds(info.alternate);
  if(kinds) text+=`\n源码目录发现旧的 ${kinds}：${info.alternate.path}。仅检测到存在，内容未核验，不会自动导入；如需使用，请在设置中显式预览。`;
  return text;
}
async function loadDataGuide(){
  const status=document.getElementById('data-status');
  const nodesStatus=document.getElementById('nodes-data-status');
  if(!status && !nodesStatus) return;
  try{
    const response=await fetch('/api/data-status',{headers:{'X-SpeedBench-Token':SB_TOKEN}});
    const info=await response.json();if(!info.ok) throw new Error('Data status unavailable');
    const kinds=alternateKinds(info.alternate);
    if(status) status.textContent=`本实例数据目录：${info.data_home}\nJSONL：${info.history.jsonl_path}（${info.history.jsonl_exists?'已有文件，沿用该位置':'尚无文件'}）\nSQLite：${info.history.database_path}（${info.history.database_exists?'已有文件':'尚无文件'}）`+
      (kinds?`\n另发现源码目录同名 ${kinds} 文件：${info.alternate.path}；未核验内容，没有自动导入。`:'');
    if(nodesStatus) nodesStatus.textContent=nodesDataStatusText(info);
  }catch(e){
    if(status) status.textContent='无法读取数据位置；没有重置或迁移文件。请检查本实例连接。';
    if(nodesStatus) nodesStatus.textContent='无法读取本实例数据位置；没有重置或迁移任何文件。请检查本实例连接后重试。';
  }
}
let pendingHistoryImport=null,historyImportBusy=false,historyImportBackup=null;
function historyImportButtons(){
  const directory=document.getElementById('history-import-directory');if(!directory) return;
  const closed=document.getElementById('history-import-closed');
  directory.disabled=historyImportBusy;closed.disabled=historyImportBusy;
  document.getElementById('btn-history-preview').disabled=historyImportBusy;
  document.getElementById('btn-history-import').disabled=historyImportBusy || !closed.checked ||
    !pendingHistoryImport?.can_apply || directory.value.trim()!==pendingHistoryImport.directory;
  document.getElementById('btn-history-rollback').disabled=historyImportBusy || !historyImportBackup;
}
async function loadHistoryImportStatus(){
  try{
    const r=await fetch('/api/history-import/status',{headers:{'X-SpeedBench-Token':SB_TOKEN}});
    const info=await r.json();historyImportBackup=info.ok && info.can_rollback?info.backup_id:null;
    if(info.ok && info.pending) document.getElementById('history-import-status').textContent='有未完成的导入，请保留私有备份并重启以恢复；恢复前不能开始新任务。';
  }catch(e){historyImportBackup=null;}
  historyImportButtons();
}
async function previewHistoryImport(){
  if(historyImportBusy) return;
  const directory=document.getElementById('history-import-directory').value.trim();
  const status=document.getElementById('history-import-status');
  pendingHistoryImport=null;historyImportBusy=true;historyImportButtons();
  status.textContent='正在只读预览所选目录…';
  try{
    const info=await post('/api/history-import/preview',{directory});
    if(!info.ok){status.textContent=info.msg || '无法预览；请检查本机路径与源程序是否已退出。';return;}
    pendingHistoryImport={...info,directory};
    status.textContent=`源目录：${info.source}\n本实例：${info.destination}\n新增历史 ${info.new_runs} 轮，已有相同记录 ${info.duplicate_runs} 轮，新增任务 ${info.new_tasks} 项，冲突 ${info.conflicts} 项。`+
      (info.ignored_database_runs?`\n源目录以 JSONL 为准，SQLite 另有 ${info.ignored_database_runs} 轮未列入此次导入。`:'')+
      (info.can_apply?'\n尚未写入。确认源程序已关闭后，可合并历史。':'\n冲突阻止合并，请检查源副本后重新预览。');
  }catch(e){status.textContent='预览连接失败；没有请求合并。请检查本实例连接后重试。';}
  finally{historyImportBusy=false;historyImportButtons();}
}
async function finishHistoryImport(action){
  if(historyImportBusy) return;
  const directory=document.getElementById('history-import-directory').value.trim();
  if(action==='apply' && (!pendingHistoryImport?.can_apply || directory!==pendingHistoryImport.directory ||
      !document.getElementById('history-import-closed').checked)) return;
  if(action==='rollback' && !historyImportBackup) return;
  const body=action==='apply'?{token:pendingHistoryImport.token}:{backup_id:historyImportBackup};
  const status=document.getElementById('history-import-status');
  historyImportBusy=true;pendingHistoryImport=null;historyImportButtons();
  status.focus?.({preventScroll:true});
  status.textContent=action==='apply'?'正在保存私有备份并合并历史…':'正在恢复本次导入前的私有备份…';
  try{
    const info=await post('/api/history-import/'+action,body);
    if(!info.ok){status.textContent=info.msg || '操作被拒绝；请保留备份并重新预览。';return;}
    clearTaskHistory();
    status.textContent=action==='apply'?`已合并历史 ${info.imported_runs} 轮、任务 ${info.imported_tasks} 项。`+
      (info.backup_id?'私有备份已保存在本实例数据目录。':'全部记录已存在，无需新增备份。'):'已撤回本次导入，恢复导入前的数据。';
    await Promise.all([loadDataGuide(),loadLatest(),loadTasks(),...(histLoaded?[loadHistory()]:[])]);
    if(currentView()==='subs') await loadSubs();
  }catch(e){status.textContent='操作连接中断，结果尚未确认；请刷新数据位置。不要删除私有备份，重启后会先核验未完成事务。';}
  finally{
    historyImportBusy=false;await loadHistoryImportStatus();historyImportButtons();
    if(document.activeElement===status) document.getElementById('btn-history-preview').focus?.({preventScroll:true});
  }
}
function initHistoryImport(){
  const directory=document.getElementById('history-import-directory');if(!directory) return;
  directory.addEventListener('input',()=>{pendingHistoryImport=null;historyImportButtons();
    document.getElementById('history-import-status').textContent='目录已改变，请重新预览。';});
  document.getElementById('history-import-closed').addEventListener('change',historyImportButtons);
  document.getElementById('btn-history-preview').addEventListener('click',previewHistoryImport);
  document.getElementById('btn-history-import').addEventListener('click',()=>{
    if(document.getElementById('btn-history-import').disabled) return;
    confirmModal('确认源目录的新旧 SpeedBench 已退出，并按刚才的预览合并历史？本实例会先保存私有备份；源偏好、身份种子和缓存不会迁入。',()=>finishHistoryImport('apply'));
  });
  document.getElementById('btn-history-rollback').addEventListener('click',()=>{
    if(document.getElementById('btn-history-rollback').disabled) return;
    confirmModal('确认撤回最近一次历史导入？只恢复这次导入前的备份；如本实例已有新数据，会拒绝撤回。',()=>finishHistoryImport('rollback'));
  });
  loadHistoryImportStatus();
}
function initNodesDataGuide(){
  const card=document.getElementById('nodes-data-guide');
  if(card) card.hidden=!SB_DESKTOP;
  if(!SB_DESKTOP) return;
  const button=document.getElementById('btn-open-history-import');
  if(!button) return;
  button.addEventListener('click',()=>{
    setHash('#/settings');
    route();
    const guide=document.getElementById('data-guide');
    const directory=document.getElementById('history-import-directory');
    if(guide && guide.scrollIntoView) guide.scrollIntoView({block:'start'});
    if(directory) directory.focus?.();
  });
}
let pendingPreferenceImport=null;
function transferPreferenceRead(key){
  if(SB_DESKTOP){if(!desktopPreferencesReady) throw new Error('Desktop preferences unavailable');return lsGet(key);}
  return localStorage.getItem(key); // Do not pretend an inaccessible store is an empty export.
}
function refreshPreferenceUI(){
  currentProfile=PROFILES.includes(lsGet('sb_profile'))?lsGet('sb_profile'):'all';
  favs=new Set(JSON.parse(lsGet('sb_favs')||'[]'));favIds=new Set(JSON.parse(lsGet('sb_favs_v2')||'[]'));
  subsDays=+(lsGet('sb_subs_days')||30)||30;
  for(const button of document.querySelectorAll('#profile-bar .pf')) button.classList.toggle('on',button.dataset.p===currentProfile);
  for(const [id,key] of [['f-mode','sb_mode'],['f-target','sb_target'],['subs-days','sb_subs_days']]){
    const input=document.getElementById(id);if(input && lsGet(key)!==null) input.value=lsGet(key);
  }
  const notifications=document.getElementById('f-notifications');if(notifications) notifications.checked=lsGet('sb_notifications')==='on';
  applyTheme();updatePendingFavorites();renderTable();renderBoard();renderNodePicker();updateTaskBudget();
}
async function applyPreferenceImport(){
  const text=document.getElementById('preference-json').value;
  const status=document.getElementById('preference-transfer-status');
  if(!pendingPreferenceImport || text!==pendingPreferenceImport.text) return;
  const incoming=pendingPreferenceImport.values;
  document.getElementById('btn-preferences-import').disabled=true;
  try{
    await preferenceWrites;
    const merged=SBPreferences.merge(SBPreferences.readValues(transferPreferenceRead),incoming);
    if(typeof SBTasks!=='undefined' && sourceCatalog.status==='ok'){
      const favorites=SBTasks.migrateFavorites(JSON.parse(merged.sb_favs||'[]'),sourceCatalog.nodes||[],JSON.parse(merged.sb_favs_v2||'[]'));
      merged.sb_favs=JSON.stringify(favorites.pending);merged.sb_favs_v2=JSON.stringify(favorites.ids);
    }
    if(SB_DESKTOP){
      const result=await post('/api/preferences',merged);
      if(!result.ok) throw new Error('桌面偏好无法安全保存；原文件未被重置');
      desktopPreferences=merged;
    }else SBPreferences.saveBrowser(merged,k=>localStorage.getItem(k),(k,v)=>localStorage.setItem(k,v),k=>localStorage.removeItem(k));
    pendingPreferenceImport=null;refreshPreferenceUI();
    status.textContent='界面偏好已导入，收藏已合并；历史、身份种子和当前测速未改变。未匹配收藏需核验。';
    toast('界面偏好已导入');
  }catch(e){status.textContent='偏好未能完整保存；未报告导入成功。请检查现有偏好，原历史未改变。';toast('偏好导入失败',false);}
}
function initReleaseSettings(){
  if(typeof SBReleases==='undefined') return;
  const status=document.getElementById('release-status'),button=document.getElementById('btn-release-check');
  if(!status || !button) return;
  let userChecked=false;
  fetch('/api/releases',{headers:{'X-SpeedBench-Token':SB_TOKEN}}).then(r=>r.json()).then(r=>{
    if(!userChecked) status.textContent=SBReleases.text(r);
  }).catch(()=>{if(!userChecked) status.textContent='本地版本信息暂不可用；未进行联网检查。';});
  button.addEventListener('click',async()=>{
    if(button.disabled) return;
    userChecked=true;
    button.disabled=true;status.textContent='正在查询官方正式 Release；不会下载或安装…';
    try{status.textContent=SBReleases.text(await post('/api/releases/check',{}));}
    catch(e){status.textContent='检查连接失败，无法确认是否需要升级。请手动查看官方 Release。';}
    finally{button.disabled=false;}
  });
  document.getElementById('btn-official-releases').addEventListener('click',()=>{
    if(SB_DESKTOP) desktopAction('releases');
    else window.open(SBReleases.URL,'_blank','noopener,noreferrer');
  });
}
function initPreferenceTransfer(){
  if(typeof SBPreferences==='undefined') return;
  const textarea=document.getElementById('preference-json'), status=document.getElementById('preference-transfer-status');
  const apply=document.getElementById('btn-preferences-import');
  if(!textarea || !status || !apply) return;
  const reset=()=>{pendingPreferenceImport=null;apply.disabled=true;};
  textarea.addEventListener('input',()=>{reset();status.textContent='内容已改变；请重新预览。不要粘贴敏感配置。';});
  document.getElementById('btn-preferences-export').addEventListener('click',()=>{
    reset();try{textarea.value=SBPreferences.exportText(transferPreferenceRead);textarea.focus?.();textarea.select?.();status.textContent='已导出白名单偏好，选中后可复制。未读取密钥、历史或身份种子。';}
    catch(e){status.textContent='现有偏好无效或过大；没有导出，请保留原值后核对。';}
  });
  document.getElementById('btn-preferences-preview').addEventListener('click',()=>{
    reset();try{
      const values=SBPreferences.parseImport(textarea.value);
      const names=JSON.parse(values.sb_favs||'[]').length,ids=JSON.parse(values.sb_favs_v2||'[]').length;
      status.textContent=`有效偏好：${Object.keys(values).length} 项；旧名称收藏 ${names}，稳定 ID 收藏 ${ids}。界面选项按导入内容更新，收藏合并；尚未保存。`;
      pendingPreferenceImport={text:textarea.value,values};apply.disabled=false;
    }catch(e){status.textContent='导入格式无效、含非白名单字段或超过限制；没有应用任何内容。';}
  });
  apply.addEventListener('click',()=>confirmModal('确认导入白名单界面选项并合并收藏？不会导入历史、密钥或身份种子，也不会修改当前测速。',applyPreferenceImport));
  document.getElementById('btn-data-refresh').addEventListener('click',()=>{loadDataGuide();loadHistoryImportStatus();});
  initHistoryImport();
  initNodesDataGuide();
  loadDataGuide();
}
function initTaskControls(){
  if(typeof SBTasks==='undefined') return;
  applyTheme();
  if(SB_DESKTOP){
    document.getElementById('desktop-settings').hidden=false;
    document.getElementById('btn-browser-audit').hidden=false;
    const notifications=document.getElementById('f-notifications');
    notifications.checked=lsGet('sb_notifications')==='on';
    notifications.addEventListener('change',()=>lsSet('sb_notifications',notifications.checked?'on':'off'));
    document.getElementById('btn-browser-audit').addEventListener('click',()=>desktopAction('browser_audit'));
  }
  const savedMode=lsGet('sb_mode'),savedTarget=lsGet('sb_target');
  if(['quick','standard','deep','ip','legacy'].includes(savedMode)) document.getElementById('f-mode').value=savedMode;
  if(['daily','download','balanced','ip','residential'].includes(savedTarget)) document.getElementById('f-target').value=savedTarget;
  getJSON('/api/task-config').then(c=>{taskConfig=c;updateTaskBudget();}).catch(()=>{});
  for(const id of ['f-source','f-mode','f-target','f-mb','f-rounds','f-multi','f-all-ip']){
    const el=document.getElementById(id);if(el) el.addEventListener('change',()=>{
      if(id==='f-mode') lsSet('sb_mode',el.value);
      if(id==='f-target') lsSet('sb_target',el.value);
      renderNodePicker();updateTaskBudget();
    });
  }
  document.getElementById('f-node-search').addEventListener('input',renderNodePicker);
  document.getElementById('node-picker').addEventListener('change',e=>{
    const id=e.target.dataset.nodeId;if(!id) return;
    if(e.target.checked) selectedNodeIds.add(id);else selectedNodeIds.delete(id);updateTaskBudget();
  });
  document.getElementById('f-theme').addEventListener('change',e=>{lsSet('sb_theme',e.target.value);applyTheme();});
  if(window.matchMedia){const media=window.matchMedia('(prefers-color-scheme: dark)');if(media.addEventListener) media.addEventListener('change',applyTheme);}
  document.getElementById('btn-tasks-refresh').addEventListener('click',loadTasks);
  document.getElementById('task-list').addEventListener('click',e=>{const item=e.target.closest('[data-job-id]');if(item) showTaskHistory(item.dataset.jobId).catch(()=>toast('读取任务失败',false));});
  document.getElementById('tbody').addEventListener('keydown',e=>{
    if(e.target.tagName==='TR' && (e.key==='Enter'||e.key===' ')){e.preventDefault();e.target.click();}
  });
  document.getElementById('hist-tbody').addEventListener('keydown',e=>{
    if(e.target.tagName==='TR' && (e.key==='Enter'||e.key===' ')){e.preventDefault();e.target.click();}
  });
  for(const id of ['subs-nodes-tbody','task-detail']){
    document.getElementById(id).addEventListener('keydown',e=>{
      if(e.target.tagName==='TR' && (e.key==='Enter'||e.key===' ')){
        e.preventDefault();gotoTrend(e.target.dataset.name,e.target.dataset.nodeId||'');
      }
    });
  }
  document.getElementById('task-detail').addEventListener('click',e=>{
    const row=e.target.closest('tr[data-name]');if(row) gotoTrend(row.dataset.name,row.dataset.nodeId||'');
  });
}

/* ==================== 启动 ==================== */
async function boot(){
  if(SB_DESKTOP){
    try{
      const response=await fetch('/api/preferences',{headers:{'X-SpeedBench-Token':SB_TOKEN}});
      const data=await response.json();
      if(!data.ok) throw new Error('Preferences unavailable');
      desktopPreferences=data.values||{};
      desktopPreferencesReady=true;
      currentProfile=PROFILES.includes(lsGet('sb_profile'))?lsGet('sb_profile'):'all';
      favs=new Set(JSON.parse(lsGet('sb_favs')||'[]'));
      favIds=new Set(JSON.parse(lsGet('sb_favs_v2')||'[]'));
      subsDays=+(lsGet('sb_subs_days')||30)||30;
    }catch(e){toast('无法读取桌面偏好；没有重置原文件',false);}
  }
  const environment=document.getElementById('leak-environment');
  if(environment && window.SPEEDBENCH_ENV?.client==='webview') environment.textContent='执行环境：系统 WebView；本次 WebRTC 结果不代表 Chrome、Edge 或 Firefox。WebView 不支持采集时只能显示无法确认。';
  init();
  initPreferenceTransfer();
  initReleaseSettings();
  if(typeof SBConfigRoot!=='undefined')rootControls=SBConfigRoot.init({document,fetch,token:SB_TOKEN,confirm:confirmModal,onChanged:async()=>{
    ++catalogRequestRevision;selectedNodeIds.clear();sourceCatalog={version:2,status:'refreshing',sources:[],nodes:[]};
    document.getElementById('f-source').value='';renderNodePicker();updateTaskBudget();
    await loadSourceCatalog();await loadCurrent();renderNodePicker();updateTaskBudget();
    if(sourceCatalog.status==='refreshing') throw Error('Catalogue unavailable');
  }});
  route();
  renderTable();      // latestData=null → 骨架屏，loadLatest 完成后替换
  updateSortArrows('th.sort', sortKey, sortAsc);
  loadLatest(); loadCurrent();
  loadSourceCatalog();
  resumeRun();        // 接管进行中的测速（若有）：恢复运行态 UI 并启动轮询
}
boot();
