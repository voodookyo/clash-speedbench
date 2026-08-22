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
// all=综合推荐(后端 score 原样) / daily=⚡日常 / download=🚀下载 / ipclean=🧼IP
const PROFILES = ['all','daily','download','ipclean'];
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

function ipHtml(ip){
  if(!ip || !ip.ok) return '-';
  let h = `${esc(ip.country_code||ip.country||'?')}·${esc(normKind(ip))}`;
  const badges = [];
  if(ip.proxy)   badges.push('<span class="tag bad">代理</span>');
  if(ip.hosting) badges.push('<span class="tag bad">托管</span>');
  if(ip.mobile)  badges.push('<span class="tag">移动</span>');
  return badges.length ? h + ' ' + badges.join('') : h;
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

/* ==================== 表格行渲染（节点/历史两视图复用） ==================== */
// opts: {readonly, currentNode, favs, expanded, selected}
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
  h += `</td><td class="mono">${r.latency_ms??'-'}</td>`;
  h += `<td class="mono">${r.median_mbps?r.median_mbps.toFixed(1):'-'}</td>`;
  h += `<td class="stars" data-name="${esc(r.name)}" title="查看 30 天趋势"><span class="sc-num">${sc==null?'-':sc.toFixed(1)}</span> ${esc(r.stars||'')}</td>`;
  h += `<td>${ipHtml(r.ip)}</td><td>${tagHtml(r.tags)}</td><td>`;
  if(!ro)
    h += isCur ? '<button class="mini" disabled>使用中</button>'
               : `<button class="mini sw" data-name="${esc(r.name)}">切换</button>`;
  h += '</td></tr>';
  if(opts.expanded) h += detailHtml(r);
  return h;
}

// 行展开详情：延迟/抖动/建连/样本/单流/多流 + 出口 IP/ASN/ISP + 趋势入口
function detailHtml(r){
  const ip = r.ip || {};
  const asn = ip.asn ? ('AS'+String(ip.asn).replace(/^AS/i,'')) : '';
  const asnTxt = asn ? esc(asn + (ip.asname?' '+ip.asname:'')) : '-';
  const cell = (k,v)=>`<div><div class="k">${k}</div><div class="v">${v}</div></div>`;
  const cells = [
    cell('抖动', r.jitter_ms!=null ? esc(r.jitter_ms)+' ms' : '-'),
    cell('建连', r.connect_ms!=null ? esc(r.connect_ms)+' ms' : '-'),
    cell('样本大小', r.sample_mb!=null ? esc(r.sample_mb)+' MB' : '-'),
    cell('单流带宽（中位）', r.median_mbps!=null ? r.median_mbps.toFixed(1)+' Mbps' : '-'),
    cell('单流带宽（最佳）', r.best_mbps!=null ? r.best_mbps.toFixed(1)+' Mbps' : '-'),
    cell('多流带宽', r.multi_mbps!=null ? r.multi_mbps.toFixed(1)+' Mbps' : '-'),
    cell('出口 IP', ip.ok ? esc(ip.exit_ip||'-') : '-'),
    cell('ASN', ip.ok ? asnTxt : '-'),
    cell('ISP', ip.ok ? esc(ip.isp||'-') : '-'),
    cell('组织', ip.ok ? esc(ip.org||'-') : '-'),
  ].join('');
  return `<tr class="detail-row"><td colspan="8"><div class="detail-grid">${cells}</div>` +
         `<div class="detail-actions"><button class="mini trend" data-name="${esc(r.name)}">📈 查看 30 天趋势</button></div></td></tr>`;
}

function skeletonRows(n){
  let h = '';
  for(let i=0;i<n;i++) h += '<tr class="skel-row"><td colspan="8"><div class="skel"></div></td></tr>';
  return h;
}

function emptyRow(text){
  return `<tr class="empty-row"><td colspan="8">${esc(text)}</td></tr>`;
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
  const q = searchText.trim().toLowerCase();
  const rows = all.filter(r=>!q || (r.name||'').toLowerCase().includes(q));
  if(!rows.length){
    tbody.innerHTML = emptyRow(`没有匹配「${searchText.trim()}」的节点`);
    return;
  }
  sortRows(rows, sortKey, sortAsc);
  tbody.innerHTML = rows.map((r,i)=>rowHtml(r, i, {
    readonly:false, currentNode, favs,
    expanded: expandedNode===r.name, selected:false,
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
// 单节点 30 天趋势：{name, pts:[{ts,v}], changes:[...], note}；name 不符时回退 histData
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
    expanded:false, selected: r.name===histSelNode,
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
      note: ipChangeNote(d.ip_changes),
    };
  }catch(e){
    nodeTrend = {name, pts:[], changes:[], note:''};  // 静默回退 histData
  }
  drawChart(); renderIpTimeline();
}

// ip_changes 是「相邻不变则合并」的变化点时间线：首条是初始 IP，之后每条算一次变化
function ipChangeNote(changes){
  if(!changes || changes.length<2) return '';
  const last = changes[changes.length-1], prev = changes[changes.length-2];
  return `出口 IP 曾变化 ${changes.length-1} 次（最近：${prev.exit_ip||'?'} → ${last.exit_ip||'?'} @ ${(last.ts||'').slice(0,16)}）`;
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
  if(!t || !t.changes || !t.changes.length){ box.innerHTML=''; return; }
  const items = t.changes.map(c=>{
    const badges = [];
    if(c.proxy)   badges.push('<span class="tag bad">代理</span>');
    if(c.hosting) badges.push('<span class="tag bad">托管</span>');
    if(c.mobile)  badges.push('<span class="tag">移动</span>');
    const asn = c.asn ? ('AS'+String(c.asn).replace(/^AS/i,'')) : '';
    const who = [asn + (c.asname?' '+c.asname:''), c.isp, c.kind].filter(Boolean).join(' · ');
    return `<div class="ip-item"><span class="ip-ts">${esc((c.ts||'').slice(0,16))}</span>` +
           `<span>${esc(c.exit_ip||'?')}${who?' · '+esc(who):''} ${badges.join('')}</span></div>`;
  }).join('');
  box.innerHTML = '<div class="card-sub">出口 IP 时间线（相邻不变已合并）</div>' + items;
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

/* ==================== hash 路由 ==================== */
const VIEWS = ['nodes','history','about'];
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
  // 搜索框：按节点名实时过滤
  document.getElementById('f-search').addEventListener('input', e=>{
    searchText = (e.target && e.target.value) || '';
    renderTable();
  });
  document.getElementById('btn-run').addEventListener('click', startRun);
  document.getElementById('btn-cancel').addEventListener('click', cancelRun);
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
  window.addEventListener('resize', ()=>{ if(currentView()==='history') drawChart(); });
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
