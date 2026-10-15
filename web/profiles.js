/* Pure scoring on normalized results; paired with speedbench_profiles.py. */
(function(root){
'use strict';
function clamp(v,lo,hi){return Math.max(lo,Math.min(hi,v));}
const KIND_ALIAS = {'住宅':'ISP/非托管','住宅IP':'ISP/非托管','机房':'机房托管','移动':'移动网络'};
function normKind(ip){
  const k = (ip && ip.kind) || '';
  return KIND_ALIAS[k] || k || '未知';
}

function intelOf(r, preferred){
  if(preferred && typeof preferred==='object') return preferred;
  if(!r) return {};
  const direct = r.ip_intel || r.intel || (r.ip && r.ip.intel);
  if(direct && typeof direct==='object' && Object.keys(direct).length) return direct;
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
  return (r.ip && typeof r.ip==='object') ? r.ip : {};
}
function classificationOf(r, preferred){
  const x = intelOf(r, preferred);
  const c = x.classification;
  return (c && typeof c === 'object' ? c.category : c) || x.category || '';
}
// IP intelligence can contain one normalized object per address family.  Keep
// the legacy ip-api object as a fallback only when no structured intelligence
// is present; in particular, hosting=false alone must never become residential.
function intelSourcesOf(r){
  if(!r || typeof r!=='object') return [];
  const sources = [], seen = new Set();
  const add = value => {
    if(!value || typeof value!=='object' || Array.isArray(value) || seen.has(value)) return;
    if(!Object.keys(value).length) return;
    seen.add(value); sources.push(value);
  };
  add(r.ip_intel); add(r.intel);
  add(r.intel_v4); add(r.intel_v6);
  if(r.ip && typeof r.ip==='object') add(r.ip.intel);
  if(!sources.length && r.ip && typeof r.ip==='object') add(r.ip);
  return sources;
}

function intelField(source, names){
  if(!source || typeof source!=='object') return undefined;
  const keys = Array.isArray(names) ? names : [names];
  const nested = [source.ipqs, source.scamalytics, source.ipinfo, source.privacy];
  for(const key of keys){
    if(source[key]!==undefined && source[key]!==null && source[key]!=='') return source[key];
    for(const obj of nested){
      if(obj && typeof obj==='object' && obj[key]!==undefined && obj[key]!==null && obj[key]!=='')
        return obj[key];
    }
  }
  return undefined;
}

function intelNumber(source, names){
  const value = intelField(source, names);
  if(value===undefined || value===null || value==='') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function intelBoolean(source, names){
  if(!source || typeof source!=='object') return null;
  const keys = Array.isArray(names) ? names : [names];
  const nested = [source.ipqs, source.scamalytics, source.ipinfo, source.privacy];
  let sawFalse = false;
  const parse = value => {
    if(value===undefined || value===null || value==='') return null;
    if(typeof value==='boolean') return value;
    if(typeof value==='number') return value!==0;
    if(typeof value==='string'){
      if(/^true$/i.test(value)) return true;
      if(/^false$/i.test(value)) return false;
    }
    return null;
  };
  for(const key of keys){
    for(const value of [source[key], ...nested.map(obj=>obj && obj[key])]){
      const parsed = parse(value);
      if(parsed===true) return true;
      if(parsed===false) sawFalse = true;
    }
  }
  return sawFalse ? false : null;
}

function profileRootNumber(r, source, names){
  const direct = intelNumber(source, names);
  if(direct!==null) return direct;
  const aggregate = intelOf(r);
  const fromAggregate = aggregate && aggregate!==source ? intelNumber(aggregate, names) : null;
  if(fromAggregate!==null) return fromAggregate;
  return intelNumber(r, names);
}

function profileRootGrade(r, source){
  const direct = intelField(source, ['ip_grade','grade']);
  if(direct!==undefined) return String(direct).toUpperCase();
  const aggregate = intelOf(r);
  const fromAggregate = aggregate && aggregate!==source ? intelField(aggregate, ['ip_grade','grade']) : undefined;
  if(fromAggregate!==undefined) return String(fromAggregate).toUpperCase();
  const root = intelField(r, ['ip_grade','grade']);
  return root===undefined ? '' : String(root).toUpperCase();
}

const IP_GRADE_SCORE = {S:95, A:85, B:70, C:50, D:20};
const IP_CATEGORY_RANK = {
  residential: 7,
  corporate: 6,
  mobile: 5.5,
  residential_proxy: 5,
  datacenter: 3.5,
  vpn_proxy: 2,
  unknown: 0,
};

function profileRiskScore(r, source){
  let score = 100, observed = false;
  const quality = profileRootNumber(r, source, ['ip_quality_score','quality_score']);
  if(quality!==null){ score=Math.min(score, clamp(quality,0,100)); observed=true; }
  const grade = IP_GRADE_SCORE[profileRootGrade(r, source)];
  if(grade!==undefined){ score=Math.min(score, grade); observed=true; }

  const ipqsFraud = intelNumber(source, ['ipqs_fraud_score']);
  const genericFraud = ipqsFraud===null ? intelNumber(source, ['fraud_score']) : ipqsFraud;
  if(genericFraud!==null){ score=Math.min(score, 100-clamp(genericFraud,0,100)); observed=true; }
  const scamFraud = intelNumber(source, ['scamalytics_score','scamalytics_fraud_score']);
  if(scamFraud!==null){ score=Math.min(score, 100-clamp(scamFraud,0,100)); observed=true; }

  const proxy = intelBoolean(source, ['proxy','is_proxy']);
  const vpn = intelBoolean(source, ['vpn','is_vpn']);
  const tor = intelBoolean(source, ['tor','is_tor']);
  const hosting = intelBoolean(source, ['hosting','is_hosting']);
  const mobile = intelBoolean(source, ['mobile','is_mobile']);
  const residentialProxy = intelBoolean(source, ['residential_proxy','is_residential_proxy','is_res_proxy']);
  const recentAbuse = intelBoolean(source, ['ipqs_recent_abuse','recent_abuse']);
  const blacklisted = intelBoolean(source, ['scamalytics_blacklisted','blacklisted','is_blacklisted_external']);
  const datacenter = intelBoolean(source, ['scamalytics_datacenter','datacenter','is_datacenter']);
  for(const value of [proxy,vpn,tor,hosting,mobile,residentialProxy,recentAbuse,blacklisted,datacenter])
    if(value!==null) observed=true;
  if(tor===true) score=Math.min(score,10);
  if(vpn===true) score=Math.min(score,20);
  if(proxy===true) score=Math.min(score,25);
  if(blacklisted===true) score=Math.min(score,20);
  if(recentAbuse===true) score=Math.min(score,30);
  if(hosting===true || datacenter===true) score=Math.min(score,45);
  if(residentialProxy===true) score=Math.min(score,55);
  if(mobile===true) score=Math.min(score,75);

  const connection = intelField(source, ['connection_type']);
  if(connection!==undefined){
    observed=true;
    if(/data.?center|hosting|server/i.test(String(connection))) score=Math.min(score,45);
    else if(/mobile|cellular/i.test(String(connection))) score=Math.min(score,75);
  }
  const risk = intelField(source, ['scamalytics_risk','risk']);
  if(risk!==undefined){
    observed=true;
    if(/very.?high|high|danger|severe/i.test(String(risk))) score=Math.min(score,35);
  }

  const cls = String(classificationOf(r, source) || 'unknown').toLowerCase();
  const classification = source && source.classification && typeof source.classification==='object'
    ? source.classification : {};
  const confidenceValue = intelNumber(source, ['confidence']) ?? intelNumber(classification, ['confidence']);
  if(confidenceValue!==null){
    observed=true;
    score=Math.min(score, clamp(confidenceValue,0,100));
  }
  if(cls==='unknown' || !cls) score=Math.min(score,35);
  if(Array.isArray(classification.conflicts) && classification.conflicts.length) score=Math.min(score,35);
  if(!observed) return 20;  // structured-but-empty/unknown is not a clean IP
  return Math.round(clamp(score,0,100)*10)/10;
}

function legacyIpCleanProfileScore(r){
  const ip = r && r.ip;
  if(!ip || !ip.ok) return null;
  if(ip.proxy) return 20;
  if(ip.hosting) return 50;
  if(ip.mobile) return 75;
  // ip-api's all-false result only means that it did not observe a negative
  // flag.  It is not enough evidence for a clean/reputable IP, so keep this
  // legacy fallback explicitly below a full score until structured
  // intelligence is available.
  return 60;
}

function ipCleanProfileScore(r){
  const sources = intelSourcesOf(r);
  if(!sources.length) return null;
  const structured = !!(r && (r.ip_intel || r.intel || r.intel_v4 || r.intel_v6 || (r.ip && r.ip.intel)));
  if(!structured) return legacyIpCleanProfileScore(r);
  const values = sources.map(source=>profileRiskScore(r, source)).filter(value=>value!==null);
  return values.length ? Math.min(...values) : 0;
}

function legacyResidentialProfileScore(r){
  const ip = r && r.ip;
  if(!ip || !ip.ok) return null;
  if(ip.proxy) return 120;
  if(ip.hosting) return 350 + 45;
  if(ip.mobile) return 550 + 75;
  const kind = normKind(ip);
  // ISP/非托管 is deliberately below structured corporate/consumer evidence:
  // it is a useful legacy hint, not proof of residential access.
  if(kind==='ISP/非托管') return 450 + 50;
  if(kind==='机房托管') return 350 + 45;
  if(kind==='代理/VPN') return 200 + 20;
  return 0;
}

function residentialSourceScore(r, source){
  const cls = String(classificationOf(r, source) || 'unknown').toLowerCase();
  const clean = profileRiskScore(r, source);
  const category = source && source.classification && typeof source.classification==='object'
    ? source.classification : {};
  const confidence = intelNumber(source, ['confidence']) ?? intelNumber(category, ['confidence']);
  const ipqsFraud = intelNumber(source, ['ipqs_fraud_score','fraud_score']);
  const scamFraud = intelNumber(source, ['scamalytics_score','scamalytics_fraud_score']);
  const explicitRisk = intelBoolean(source, ['proxy','vpn','tor','ipqs_recent_abuse','recent_abuse',
                                              'scamalytics_blacklisted','blacklisted'])===true;
  const highRisk = (cls!=='unknown' && clean!==null && clean<40) ||
    ipqsFraud!==null && ipqsFraud>=75 || scamFraud!==null && scamFraud>=75 ||
    explicitRisk;
  // A high-risk/abuse signal is intentionally below even a clean VPN result.
  if(highRisk) return 100 + (clean===null ? 0 : clean);
  let rank = IP_CATEGORY_RANK[cls] || 0;
  if(cls==='residential') rank = confidence!==null && confidence>=80 ? 7 : 5.8;
  const suffix = clean===null ? 0 : clean;
  return rank*100 + suffix;
}

function residentialProfileScore(r){
  const sources = intelSourcesOf(r);
  if(!sources.length) return null;
  const structured = !!(r && (r.ip_intel || r.intel || r.intel_v4 || r.intel_v6 || (r.ip && r.ip.intel)));
  if(!structured) return legacyResidentialProfileScore(r);
  // Use the least favorable address family so a datacenter/abuse IPv6 cannot
  // silently hide behind a clean IPv4 result.
  return Math.min(...sources.map(source=>residentialSourceScore(r, source)));
}

// 各 Profile 的评分公式；返回 null 表示"不通/无数据"，排序时统一沉底
function score(r, profile){
  switch(profile){
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
    case 'ip': case 'ipclean': {  // 🧼IP = 出口 IP 属性分，取最差标记
      return ipCleanProfileScore(r);
    }
    case 'residential': {  // 🏠住宅优先：多源分类/Grade，未知永远沉底
      return residentialProfileScore(r);
    }
    default: return r.score;  // 综合推荐：后端分数原样
  }
}


function coverage(r,profile){
  const scope=r.measurement_scope||{};
  if(!['all','balanced','daily','download'].includes(profile) || !['quick','standard','deep'].includes(scope.mode))return 0;
  if(scope.bandwidth==='partial')return intelNumber(r,['median_mbps'])!==null?2:0;
  return {completed:3,failed:1}[scope.bandwidth]||0;
}
function compareText(a,b){
  const left=[...a],right=[...b];
  for(let i=0;i<Math.min(left.length,right.length);i++){
    const difference=left[i].codePointAt(0)-right[i].codePointAt(0);if(difference)return difference;
  }return left.length-right.length;
}
function compare(a,b,profile,asc=false){
  const cov=coverage(b,profile)-coverage(a,profile);if(cov)return cov;
  const av=score(a,profile),bv=score(b,profile);
  if(av==null && bv!=null)return 1;if(bv==null && av!=null)return -1;
  if(av!=null && bv!=null && av!==bv)return asc?av-bv:bv-av;
  const lat=(a.latency_ms??Infinity)-(b.latency_ms??Infinity);
  if(Number.isFinite(lat) && lat)return lat;
  if(a.latency_ms==null && b.latency_ms!=null)return 1;
  if(b.latency_ms==null && a.latency_ms!=null)return -1;
  const key=r=>r.measurement_scope?.mode?[r.node_id||'',r.name||'']:['',''];
  const ak=key(a),bk=key(b);
  for(let i=0;i<2;i++){const difference=compareText(ak[i],bk[i]);if(difference)return difference;}return 0;
}
const api={score,compare,coverage,intelOf,classificationOf,normKind};
if(typeof module!=='undefined'&&module.exports)module.exports=api;
root.SBProfiles=api;
})(typeof globalThis!=='undefined'?globalThis:this);
