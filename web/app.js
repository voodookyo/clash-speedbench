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
  return r.json();
}

// 一切进入 innerHTML 的动态文本必须过 esc
function esc(s){ return (s??'').toString().replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

// localStorage 在某些隐私模式下会抛异常，包一层静默降级
function lsGet(k){ try{ return localStorage.getItem(k); }catch(e){ return null; } }
function lsSet(k,v){ try{ localStorage.setItem(k,v); }catch(e){} }

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
let modalYes = null;
function confirmModal(text, onYes){
  document.getElementById('modal-text').textContent = text;
  modalYes = onYes;
  document.getElementById('modal-mask').style.display = 'flex';
}
function closeModal(){
  document.getElementById('modal-mask').style.display = 'none';
  modalYes = null;
}

/* ==================== 评分 Profile（公式与旧版一致，勿改） ==================== */
// all=综合推荐(后端 score 原样) / daily=⚡日常 / download=🚀下载 /
// ipclean=🧼IP / residential=🏠住宅优先。下载 Profile 不读取 IP 规则。
const PROFILES = ['all','daily','download','ipclean','residential'];
let currentProfile = lsGet('sb_profile');
if(!PROFILES.includes(currentProfile)) currentProfile = 'all';

// 各 Profile 的评分公式；返回 null 表示"不通/无数据"，排序时统一沉底
function profileScore(r){
  switch(currentProfile){
    case 'daily': {  // ⚡日常 = 0.5×latScore + 0.3×jitterScore + 0.2×bwScore
      if(r.latency_ms==null) return null;
      const lat = 100*clamp((800-r.latency_ms)/(800-80), 0, 1);   // ≤80ms→100，≥800ms→0
      // 老数据没有 jitter 字段时按中性 50 计，不至于整行沉底
      const jit = r.jitter_ms==null ? 50
                  : 100*clamp((200-r.jitter_ms)/(200-10), 0, 1);  // ≤10ms→100，≥200ms→0
      const bw  = Math.min(r.median_mbps||0, 100);                // min(mbps,100)
      return 0.5*lat + 0.3*jit + 0.2*bw;
    }
    case 'download': {  // 🚀下载 = 0.7×bwScore + 0.3×multiScore
      if(r.median_mbps==null) return null;
      const bw = Math.min(r.median_mbps,300)/300*100;             // 300M 封顶
      const multi = r.multi_mbps ?? r.median_mbps;                // multi 缺失用 median 兜底
      const ms = Math.min(multi,500)/500*100;                     // 500M 封顶
      return 0.7*bw + 0.3*ms;
    }
    case 'ipclean': {  // 🧼IP = 出口 IP 属性分，取最差标记
      const ip = r.ip;
      if(!ip || !ip.ok) return null;  // 查询失败/未知：沉底（用 null 而非 0，升序时也沉底）
      if(ip.proxy)   return 20;
      if(ip.hosting) return 50;
      if(ip.mobile)  return 75;
      return 100;
    }
    case 'residential': {  // 🏠住宅优先：多源分类/Grade，未知永远沉底
      const ip = r.ip_intel || r.intel || r.ip || {};
      const cls = ip.classification && typeof ip.classification === 'object'
        ? ip.classification.category : (ip.classification || ip.category || 'unknown');
      const rank = {residential:7, corporate:6, residential_proxy:5,
                    mobile:4, datacenter:3, vpn_proxy:2, unknown:0};
      const grade = {S:5, A:4, B:3, C:2, D:1};
      const g = grade[String(ip.ip_grade || ip.grade || '').toUpperCase()] || 0;
      return (rank[cls] || 0)*10 + g;
    }
    default: return r.score;  // 综合推荐：后端分数原样
  }
}

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
const KIND_ALIAS = {'住宅':'ISP/非托管','住宅IP':'ISP/非托管','机房':'机房托管','移动':'移动网络'};
function normKind(ip){
  const k = (ip && ip.kind) || '';
  return KIND_ALIAS[k] || k || '未知';
}

const INTEL_KIND_LABEL = {
  residential: '住宅 ISP',
  residential_proxy: 'ISP住宅代理',
  corporate: '企业/商宽',
  mobile: '移动网络',
  datacenter: '数据中心',
  vpn_proxy: '代理/VPN',
  unknown: '未知',
};
function intelOf(r){
  if(!r) return {};
  const direct = r.ip_intel || r.intel || (r.ip && r.ip.intel);
  if(direct) return direct;
  // The serialized Result model keeps one full intelligence object per
  // address family.  Prefer IPv4 for the compact row, while carrying the
  // result-level grade/score into the detail renderer.
  const family = r.intel_v4 || r.intel_v6;
  if(family){
    return Object.assign({}, family, {
      ip_quality_score: r.ip_quality_score ?? family.ip_quality_score,
      ip_grade: r.ip_grade || family.ip_grade,
      exit_ipv4: r.exit_ipv4 || family.ip,
      exit_ipv6: r.exit_ipv6 || (r.intel_v6 && r.intel_v6.ip),
    });
  }
  return {};
}
function classificationOf(r){
  const x = intelOf(r);
  const c = x.classification;
  return (c && typeof c === 'object' ? c.category : c) || x.category || '';
}
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
    if(arr) arr.textContent = (th.dataset && th.dataset.k===key) ? (asc?'▲':'▼') : '';
  }
}

/* ==================== 全局状态 ==================== */
let latestData = null;        // null=加载中（骨架屏）；{}=无记录
let sortKey = 'score', sortAsc = false;
let currentNode = '', currentGroup = '';
let searchText = '';
let expandedNode = null;      // 节点视图中展开详情面板的节点（一次只展开一个）
let pollTimer = null;         // 测速状态轮询：全局单例，切视图不清除

/* ==================== 表格行渲染（节点/历史/订阅三视图复用） ==================== */
// opts: {readonly, currentNode, favs, expanded, selected, provider, cols}
function rowHtml(r, i, opts){
  const ro = opts.readonly;
  const isCur = !ro && r.name===opts.currentNode;
  const isFav = !ro && opts.favs.has(r.name);
  const sc = profileScore(r);
  // 评分列 = 当前 Profile 分数（一位小数）+ 星标。星标始终显示后端 stars：
  // stars 是后端综合评级的直观符号，Profile 切换只改数值与排序，不同步换算以免误导。
  let h = `<tr data-name="${esc(r.name)}"${isCur?' class="current"':''}${opts.selected?' class="sel"':''}>`;
  h += `<td>${i+1}</td><td>`;
  if(!ro)
    h += `<span class="fav${isFav?' on':''}" data-name="${esc(r.name)}" title="收藏/取消收藏">${isFav?'★':'☆'}</span>`;
  h += esc(r.name);
  if(isCur) h += '<span class="cur-mark">✅ 使用中</span>';
  h += '</td>';
  if(opts.provider) h += `<td class="mono">${esc(r.provider||'(未知订阅)')}</td>`;
  h += `<td class="mono">${r.latency_ms??'-'}</td>`;
  h += `<td class="mono">${r.median_mbps?r.median_mbps.toFixed(1):'-'}</td>`;
  const network = r.network_score ?? r.networkScore ?? r.score;
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
               : `<button class="mini sw" data-name="${esc(r.name)}">切换</button>`;
  h += '</td></tr>';
  if(opts.expanded) h += detailHtml(r, opts.cols||8);
  return h;
}

// 行展开详情：延迟/抖动/建连/样本/单流/多流 + 出口 IP/ASN/ISP + 趋势入口
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
    cell('应用层探测失败率', r.probe_loss_pct!=null ? esc(r.probe_loss_pct)+'%' : '-'),
    cell('出口 IP', ip.ok ? esc(ip.exit_ip||'-') : '-'),
    cell('IPv4', esc(r.exit_ipv4 || intel.exit_ipv4 || (ip.ok ? ip.exit_ip : '') || '-')),
    cell('IPv6', esc(r.exit_ipv6 || intel.exit_ipv6 || '-')),
    cell('ASN', asnTxt),
    cell('ISP', isp ? esc(isp) : '-'),
    cell('组织', organization ? esc(organization) : '-'),
    cell('IP 类型', esc(classificationLabel(r))),
    cell('Confidence', cls.confidence!=null ? esc(cls.confidence)+'%' : '-'),
    cell('IP Grade', esc(ipGradeOf(r))),
    cell('IPQS Fraud', intel.ipqs_fraud_score!=null ? esc(intel.ipqs_fraud_score) : '-'),
    cell('Scamalytics Fraud', intel.scamalytics_score!=null ? esc(intel.scamalytics_score) : '-'),
  ].join('');
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
  return `<tr class="detail-row"><td colspan="${colspan||8}"><div class="detail-grid">${cells}</div>${intelText}` +
         `<div class="detail-actions"><button class="mini trend" data-name="${esc(r.name)}">📈 查看 30 天趋势</button></div></td></tr>`;
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
  const all = latestData.results || [];
  if(!all.length){
    tbody.innerHTML = emptyRow('暂无测速记录 · 在上方设置参数后点击「开始测速」');
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
  tbody.innerHTML = rows.map((r,i)=>rowHtml(r, i, {
    readonly:false, currentNode, favs,
    expanded: expandedNode===r.name, selected:false,
    provider: showProv, cols,
  })).join('');
}

function renderMeta(){
  document.getElementById('cur-line').textContent =
    currentNode ? `当前：${currentGroup} = ${currentNode}` : '';
  renderTable();
}

function setSort(k){
  if(sortKey===k){ sortAsc=!sortAsc; } else { sortKey=k; sortAsc=(k==='name'||k==='latency_ms'); }
  updateSortArrows('th.sort', sortKey, sortAsc);
  renderTable();
}

/* ---------- 地区榜 ---------- */
function boardItem(x){
  return `<span class="board-item" data-name="${esc(x.name)}">${esc(x.name)} <b>${x.sc==null?'-':x.sc.toFixed(1)}</b>${x.mbps!=null?`·${x.mbps.toFixed(0)}M`:''}</span>`;
}

// 地区榜：按 regionOf 分组，每组取当前 Profile 下 Top 3；不通/无数据（分数 null）不进榜；
// 顶部固定一行「⭐ 收藏」速览（无收藏时不显示）；整榜在无数据时隐藏
function renderBoard(){
  const bd = document.getElementById('board');
  if(!latestData || !latestData.results || !latestData.results.length){ bd.style.display='none'; return; }
  let html = '';
  const favRows = latestData.results.filter(r=>favs.has(r.name))
    .map(r=>({name:r.name, sc:profileScore(r), mbps:r.median_mbps}))
    .sort((a,b)=>(b.sc??-1)-(a.sc??-1));
  if(favRows.length)
    html += `<div class="board-group"><span class="board-code">⭐ 收藏</span>${favRows.map(boardItem).join('')}</div>`;
  const groups = {};
  for(const r of latestData.results){
    const sc = profileScore(r);
    if(sc==null) continue;
    const code = regionOf(r);
    if(!groups[code]) groups[code] = [];
    groups[code].push({name:r.name, sc, mbps:r.median_mbps});
  }
  const codes = Object.keys(groups).sort((a,b)=>{
    const top = c=>Math.max(...groups[c].map(x=>x.sc));
    return top(b)-top(a) || a.localeCompare(b);
  });
  for(const c of codes){
    const top3 = groups[c].sort((a,b)=>b.sc-a.sc).slice(0,3);
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
let histSelNode = null;
// 单节点 30 天趋势：{name, pts:[{ts,v}], changes:[...], reputation_changes:[...], note}；
// name 不符时回退 histData
let nodeTrend = null;

// 一轮测速的冠军：后端综合评分最高者（null 跳过）
function championOf(rec){
  let best = null;
  for(const r of (rec.results||[])){
    if(r.score==null) continue;
    if(!best || r.score>best.score) best = r;
  }
  return best;
}

async function loadHistory(){
  try{ histData = await getJSON('/api/history'); }
  catch(e){ histData = []; toast('读取历史记录失败', false); }
  histLoaded = true;
  if(histData.length && (histSelRun<0 || histSelRun>=histData.length)){
    histSelRun = histData.length-1;   // 默认最新一轮 + 冠军节点
    const ch = championOf(histData[histSelRun]);
    histSelNode = ch ? ch.name : ((histData[histSelRun].results||[])[0]||{}).name || null;
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
    html += `<div class="hist-item${i===histSelRun?' on':''}" data-i="${i}">` +
            `<div class="hist-ts">${esc(rec.ts||'')}</div><div class="hist-sub">${sub}</div></div>`;
  }
  box.innerHTML = html;
}

function renderHistTable(){
  const tbody = document.getElementById('hist-tbody');
  const rec = histData[histSelRun];
  if(!rec){ tbody.innerHTML = emptyRow('暂无数据'); return; }
  document.getElementById('hist-run-title').textContent = `本轮结果：${rec.ts}（只读，点击行看趋势）`;
  const rows = (rec.results||[]).slice();
  if(!rows.length){ tbody.innerHTML = emptyRow('该轮没有节点数据'); return; }
  sortRows(rows, histSortKey, histSortAsc);
  tbody.innerHTML = rows.map((r,i)=>rowHtml(r, i, {
    readonly:true, currentNode:'', favs:{has(){return false}},
    expanded:false, selected: r.name===histSelNode, intelColumns:false,
  })).join('');
}

function setHistSort(k){
  if(histSortKey===k){ histSortAsc=!histSortAsc; } else { histSortKey=k; histSortAsc=(k==='name'||k==='latency_ms'); }
  updateSortArrows('th.hsort', histSortKey, histSortAsc);
  renderHistTable();
}

/* ---------- 单节点 30 天趋势 + IP 变化 ---------- */
// 数据源：优先 /api/node 的 30 天序列（含 IP 变化时间线），失败回退 histData
async function fetchNodeTrend(name){
  nodeTrend = null;
  drawChart();   // 先用 histData 画兜底版
  renderIpTimeline();
  try{
    const d = await getJSON('/api/node?name='+encodeURIComponent(name)+'&days=30');
    if(histSelNode!==name) return;  // 等待期间用户已改选别的节点，丢弃过期响应
    nodeTrend = {
      name,
      pts: (d.series||[]).filter(s=>s.median_mbps!=null)
           .map(s=>({ts:(s.ts||'').slice(5,16), v:s.median_mbps})),
      changes: d.ip_changes||[],
      reputation_changes: Array.isArray(d.ip_reputation_changes)
        ? d.ip_reputation_changes : [],
      note: ipChangeNote(d.ip_changes),
    };
  }catch(e){
    nodeTrend = {name, pts:[], changes:[], reputation_changes:[], note:''};  // 静默回退 histData
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
  const useSeries = nodeTrend && nodeTrend.name===name && nodeTrend.pts.length;
  document.getElementById('chart-sub').textContent = useSeries ? (nodeTrend.note||'') : '';
  const pts = [];
  if(useSeries){
    pts.push(...nodeTrend.pts);
  }else if(name && histData.length){
    for(const rec of histData){
      const r = (rec.results||[]).find(x=>x.name===name);
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
  const t = nodeTrend && nodeTrend.name===histSelNode ? nodeTrend : null;
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

async function loadSubs(){
  let d;
  try{ d = await getJSON('/api/subscriptions?days='+subsDays); }
  catch(e){ d = []; toast('读取订阅汇总失败', false); }
  subsData = Array.isArray(d) ? d : [];
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
  tbody.innerHTML = subsData.map(s=>
    `<tr data-provider="${esc(s.provider)}"${s.provider===subsSel?' class="sel"':''}>` +
    `<td>${esc(s.provider)}</td>` +
    `<td class="mono">${s.run_count}</td>` +
    `<td class="mono">${s.node_count}</td>` +
    `<td class="mono">${s.online_ratio==null?'-':(s.online_ratio*100).toFixed(0)+'%'}</td>` +
    `<td class="mono">${s.median_mbps!=null?s.median_mbps.toFixed(1):'-'}</td>` +
    `<td class="mono">${s.latency_ms!=null?s.latency_ms.toFixed(0):'-'}</td>` +
    `<td class="mono">${s.avg_score!=null?s.avg_score.toFixed(1):'-'}</td>` +
    `<td class="mono">${esc((s.last_ts||'').slice(0,16))}</td></tr>`
  ).join('');
}

async function selectSub(name){
  subsSel = name;
  document.getElementById('subs-detail-card').style.display = '';
  document.getElementById('subs-detail-title').textContent =
    `订阅趋势：${name}（近 ${subsDays} 天，三条线各自归一）`;
  renderSubsTable();
  subsSeries = null;
  drawSubsChart();
  try{
    const d = await getJSON('/api/subscription?name='+encodeURIComponent(name)+'&days='+subsDays);
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
  let rec = null;
  for(let i=hist.length-1;i>=0;i--){
    if((hist[i].results||[]).some(r=>(r.provider||'')===want)){ rec = hist[i]; break; }
  }
  document.getElementById('subs-chart-sub').textContent =
    rec ? `最近一轮：${rec.ts}` : '';
  const rows = rec ? (rec.results||[]).filter(r=>(r.provider||'')===want) : [];
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
  try{
    const r = await getJSON('/api/current');
    if(r.ok){ currentGroup=r.group; currentNode=r.now; }
  }catch(e){}
  renderMeta();
}

/* ==================== 测速控制 ==================== */
function setRunUi(running){
  document.getElementById('btn-run').disabled = running;
  document.getElementById('btn-cancel').style.display = running ? '' : 'none';
  document.getElementById('prog-wrap').style.display = running ? 'flex' : 'none';
  if(running){
    document.getElementById('log-toggle').style.display = '';
    document.getElementById('log').style.display = 'block';
    document.getElementById('log-arrow').textContent = '▾';
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
  setRunUi(true);
  startPolling();
}

async function startRun(){
  const body = {
    include: document.getElementById('f-include').value,
    mb: +document.getElementById('f-mb').value,
    rounds: +document.getElementById('f-rounds').value,
    auto_switch: document.getElementById('f-autoswitch').checked,
  };
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
async function switchNode(name, btn){
  if(btn){ btn.disabled = true; btn.textContent = '切换中…'; }
  let r;
  try{ r = await post('/api/switch', {name}); }
  catch(e){ toast('切换请求失败', false); renderTable(); return; }
  if(r.ok){
    currentNode = name;
    if(r.group) currentGroup = r.group;
    toast(r.msg || `已切换 → ${name}`);
    renderMeta();
  }else{
    toast(r.msg||'切换失败', false);
    renderTable();
  }
}

// 「查看 30 天趋势」：跳到历史视图并选中该节点（含该节点的最近一轮）
function gotoTrend(name){
  setHash('#/history');   // 触发 hashchange → route()
  const go = ()=>{
    for(let i=histData.length-1;i>=0;i--){
      if((histData[i].results||[]).some(x=>x.name===name)){ histSelRun=i; break; }
    }
    histSelNode = name;
    renderHistList(); renderHistTable(); fetchNodeTrend(name);
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
  if(run){ run.disabled = true; run.textContent = '检测中…'; }
  setLeakStatus({status:'unknown', status_text:'正在采集 WebRTC candidate…'});
  try{
    const [exit_ipv4, exit_ipv6, gathered] = await Promise.all([
      browserExitIp('https://api.ipify.org?format=json'),
      browserExitIp('https://api6.ipify.org?format=json'),
      collectWebRTCCandidates(),
    ]);
    const payload = Object.assign({}, gathered, {exit_ipv4, exit_ipv6});
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
  box.innerHTML = rows.map(x=>`<div class="history-chip"><b>${esc(x.created_at||x.ts||'-')}</b> · ${esc(x.webrtc_status||'unknown')} · DNS ${esc(x.dns_status||'unknown')}</div>`).join('');
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

function openDnsAudit(url){
  // noopener/noreferrer is explicit; the target pages are never scraped.
  try{ const child=window.open(url, '_blank', 'noopener,noreferrer'); if(child) child.opener=null; }
  catch(e){}
}

/* ==================== hash 路由 ==================== */
const VIEWS = ['nodes','history','subs','leak','settings','about'];
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
  for(const a of navs){ if(a.classList) a.classList.toggle('on', a.dataset && a.dataset.view===v); }
  if(v==='history'){
    if(!histLoaded) loadHistory(); else drawChart();   // 切回时 canvas 已有宽度，重画
  }
  if(v==='subs'){
    if(!subsLoaded) loadSubs(); else if(subsSel) drawSubsChart();
  }
  if(v==='leak') loadLeakHistory();
  if(v==='settings') loadProviderStatus();
}

/* ==================== 事件绑定（全部 addEventListener/委托） ==================== */
function init(){
  // 节点表格：事件委托；节点名一律走 dataset（HTML 属性经 esc 转义），绝不拼接进 JS 源码
  document.getElementById('tbody').addEventListener('click', e=>{
    const fv = e.target.closest('.fav');
    if(fv && fv.dataset.name!=null){ toggleFav(fv.dataset.name); return; }
    const sw = e.target.closest('button.sw');
    if(sw && sw.dataset.name!=null){ switchNode(sw.dataset.name, sw); return; }
    const tb = e.target.closest('button.trend');
    if(tb && tb.dataset.name!=null){ gotoTrend(tb.dataset.name); return; }
    const cell = e.target.closest('td.stars');
    if(cell && cell.dataset.name!=null){ gotoTrend(cell.dataset.name); return; }
    const tr = e.target.closest('tr[data-name]');
    if(tr && tr.dataset.name!=null){   // 点击行：展开/收起详情面板
      expandedNode = (expandedNode===tr.dataset.name) ? null : tr.dataset.name;
      renderTable();
    }
  });
  // 历史表格（只读）：点击行选中节点看趋势
  document.getElementById('hist-tbody').addEventListener('click', e=>{
    const tr = e.target.closest('tr[data-name]');
    if(tr && tr.dataset.name!=null){
      histSelNode = tr.dataset.name;
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
      histSelNode = ch ? ch.name : (((rec&&rec.results)||[])[0]||{}).name || null;
      renderHistList(); renderHistTable();
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
  document.getElementById('board-toggle').addEventListener('click', ()=>{
    const body = document.getElementById('board-body');
    const open = body.style.display==='none';
    body.style.display = open?'':'none';
    document.getElementById('board-arrow').textContent = open?'▾':'▸';
  });
  document.getElementById('board-body').addEventListener('click', e=>{
    const it = e.target.closest('.board-item');
    if(it && it.dataset.name!=null) gotoTrend(it.dataset.name);
  });
  // 运行日志折叠
  document.getElementById('log-toggle').addEventListener('click', ()=>{
    const log = document.getElementById('log');
    const open = log.style.display==='none';
    log.style.display = open?'block':'none';
    document.getElementById('log-arrow').textContent = open?'▾':'▸';
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
    if(tr && tr.dataset.provider!=null) selectSub(tr.dataset.provider);
  });
  document.getElementById('subs-nodes-tbody').addEventListener('click', e=>{
    const cell = e.target.closest('td.stars');
    if(cell && cell.dataset.name!=null) gotoTrend(cell.dataset.name);
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
  window.addEventListener('hashchange', route);
  window.addEventListener('resize', ()=>{
    if(currentView()==='history') drawChart();
    if(currentView()==='subs') drawSubsChart();
  });
}

/* ==================== 启动 ==================== */
function boot(){
  init();
  route();
  renderTable();      // latestData=null → 骨架屏，loadLatest 完成后替换
  updateSortArrows('th.sort', sortKey, sortAsc);
  loadLatest(); loadCurrent();
  resumeRun();        // 接管进行中的测速（若有）：恢复运行态 UI 并启动轮询
}
boot();
