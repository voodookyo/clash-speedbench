#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clash SpeedBench 本地 Web 面板
- Zero-dependency: stdlib http.server only, all HTML/CSS/JS embedded
- Start a benchmark, watch live progress, browse results, switch nodes
- Binds 127.0.0.1 only.

Usage: python3 speedbench_web.py [--port 8950]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from clash_speedbench import (  # noqa: E402
    MihomoAPI,
    build_selectable_graph,
    detect_controller,
    pick_switch_group,
)
import speedbench_db  # noqa: E402

SCRIPT = HERE / "clash_speedbench.py"
# 数据目录：默认脚本同级；打包成 .app 时由启动器用 SPEEDBENCH_HOME 指到
# ~/Library/Application Support/ClashSpeedBench，避免污染应用包。
DATA_HOME = Path(os.environ.get("SPEEDBENCH_HOME", str(HERE)))
HISTORY = DATA_HOME / "speedbench-history.jsonl"


def db_path() -> Path:
    """SQLite 历史库：与 jsonl 同目录同名、仅换后缀。

    jsonl 仍是原始备份，DB 是它的可查询镜像；测试补丁 HISTORY 时
    DB 路径随之指向临时目录，互不串扰。
    """
    return HISTORY.with_suffix(".db")


STATE = {
    "running": False,
    "lines": [],
    "started": None,
    "exit_code": None,
    "proc": None,
}
STATE_LOCK = threading.Lock()

MAX_LINES = 500

# 每次启动随机生成的写操作令牌：注入页面 <meta>，所有 POST 必须携带，
# 防止其他网页跨站向本地面板发写请求（CSRF）。
WEB_TOKEN = secrets.token_hex(16)

# jsonl → DB 的同步策略：读取前惰性增量同步。每次读 API 先比对 jsonl mtime，
# 有变化才 import_jsonl（导入本身按 ts 去重，幂等），面板读到的永远是最新数据，
# mtime 不变时代价只是一次 stat；启动时与 /api/run 结束后再各显式同步一次，
# 只为让导入问题尽早暴露。
_DB_SYNC_LOCK = threading.Lock()
_DB_SYNCED = {}  # str(db_path) -> 已同步的 jsonl mtime


def sync_db() -> int:
    """jsonl 有新增时增量导入 SQLite（幂等），返回新导入的轮次数。"""
    try:
        mtime = HISTORY.stat().st_mtime
    except OSError:
        return 0  # 历史文件不存在：无可导入，查询会返回空
    key = str(db_path())
    with _DB_SYNC_LOCK:
        if _DB_SYNCED.get(key) == mtime and Path(key).exists():
            return 0
        n = speedbench_db.import_jsonl(db_path(), HISTORY)
        _DB_SYNCED[key] = mtime
        return n


# 旧 jsonl 直读：保留作应急回退与兼容测试用；Web API 已改走 SQLite（见下）。
def read_history() -> list:
    if not HISTORY.exists():
        return []
    records = []
    with HISTORY.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def latest_record() -> dict:
    sync_db()
    return speedbench_db.latest_run(db_path())


def slim_history() -> list:
    """Trend data: per run, per node, only the fields the chart needs."""
    sync_db()
    out = []
    for rec in speedbench_db.all_runs(db_path()):
        out.append({
            "ts": rec.get("ts", ""),
            "results": [
                {
                    "name": r.get("name"),
                    "median_mbps": r.get("median_mbps"),
                    "latency_ms": r.get("latency_ms"),
                    "score": r.get("score"),
                }
                for r in rec.get("results", [])
            ],
        })
    return out


def run_benchmark(params: dict) -> None:
    cmd = [sys.executable, str(SCRIPT), "--yes", "--history", str(HISTORY)]
    if params.get("include"):
        cmd += ["--include", str(params["include"])]
    if params.get("mb"):
        cmd += ["--mb", str(int(params["mb"]))]
    if params.get("rounds"):
        cmd += ["--rounds", str(int(params["rounds"]))]
    if params.get("auto_switch"):
        cmd += ["--auto-switch"]

    with STATE_LOCK:
        STATE["running"] = True
        STATE["lines"] = ["$ " + " ".join(os.path.basename(c) if c == str(SCRIPT) else c for c in cmd)]
        STATE["started"] = time.time()
        STATE["exit_code"] = None

    try:
        proc = subprocess.Popen(
            cmd, cwd=str(DATA_HOME),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        with STATE_LOCK:
            STATE["proc"] = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            with STATE_LOCK:
                STATE["lines"].append(line)
                if len(STATE["lines"]) > MAX_LINES:
                    STATE["lines"] = STATE["lines"][-MAX_LINES:]
        STATE["exit_code"] = proc.wait()
    except Exception as e:
        with STATE_LOCK:
            STATE["lines"].append(f"!! 启动测速失败: {e}")
            STATE["exit_code"] = -1
    finally:
        with STATE_LOCK:
            STATE["running"] = False
            STATE["proc"] = None
        # 测速进程已把本轮结果追加进 jsonl，顺手增量入库；失败不影响面板状态
        try:
            sync_db()
        except Exception as e:
            with STATE_LOCK:
                STATE["lines"].append(f"!! 历史入库失败: {e}")


def cancel_benchmark() -> dict:
    """中断正在运行的测速子进程。

    先发 SIGINT：clash_speedbench.py 的 finally 会恢复 Clash 策略组/模式；
    最多等 5 秒，未退出再 terminate（再兜底 kill）。
    """
    with STATE_LOCK:
        proc = STATE.get("proc")
        running = STATE["running"]
    if not running or proc is None or proc.poll() is not None:
        return {"ok": False, "msg": "当前没有正在进行的测速"}
    try:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        with STATE_LOCK:
            STATE["lines"].append("!! 测速已被手动中断")
        return {"ok": True, "msg": "已中断测速，Clash 配置已恢复"}
    except Exception as e:
        return {"ok": False, "msg": f"中断失败: {e}"}


def do_switch(name: str) -> dict:
    try:
        base, needs_secret = detect_controller(os.environ.get("MIHOMO_SECRET", ""), None)
        if needs_secret:
            return {"ok": False, "msg": "Controller 需要 Secret，请设置环境变量 MIHOMO_SECRET 后再试"}
        api = MihomoAPI(base, secret=os.environ.get("MIHOMO_SECRET", ""))
        proxies = api.get("/proxies").get("proxies", {})
        graph = build_selectable_graph(proxies)
        group = pick_switch_group(proxies, graph, name, "GLOBAL")
        if not group:
            return {"ok": False, "msg": f"找不到包含 {name} 的 Selector 组"}
        current = proxies.get(group, {}).get("now")
        if current == name:
            return {"ok": True, "msg": f"{group} 已是 {name}", "group": group, "now": name}
        api.select(group, name)
        return {"ok": True, "msg": f"已切换 {group} → {name}", "group": group, "now": name}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def get_current() -> dict:
    """The main selector group (largest non-GLOBAL Selector) and its current node."""
    try:
        base, needs_secret = detect_controller(os.environ.get("MIHOMO_SECRET", ""), None)
        if needs_secret:
            return {"ok": False, "msg": "需要 Secret"}
        api = MihomoAPI(base, secret=os.environ.get("MIHOMO_SECRET", ""))
        proxies = api.get("/proxies").get("proxies", {})
        graph = build_selectable_graph(proxies)
        cands = [g for g in graph
                 if g != "GLOBAL" and proxies.get(g, {}).get("type") == "Selector"]
        group = max(cands, key=lambda g: len(graph[g])) if cands else "GLOBAL"
        return {"ok": True, "group": group,
                "now": str(proxies.get(group, {}).get("now", ""))}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="sb-token" content="__SB_TOKEN__">
<title>Clash SpeedBench</title>
<style>
  :root { --bg:#0d1117; --card:#161b22; --border:#30363d; --fg:#e6edf3;
          --dim:#8b949e; --accent:#58a6ff; --good:#3fb950; --bad:#f85149; }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px; background:var(--bg); color:var(--fg);
         font:14px/1.5 -apple-system, "SF Pro", "PingFang SC", sans-serif; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:var(--dim); margin-bottom:20px; }
  .card { background:var(--card); border:1px solid var(--border);
          border-radius:10px; padding:16px; margin-bottom:20px; }
  .controls { display:flex; gap:10px; flex-wrap:wrap; align-items:end; }
  label { display:block; color:var(--dim); font-size:12px; margin-bottom:4px; }
  input[type=text], input[type=number] { background:#0d1117; color:var(--fg);
    border:1px solid var(--border); border-radius:6px; padding:6px 10px; width:180px; }
  input[type=number] { width:80px; }
  button { background:var(--accent); color:#0d1117; border:0; border-radius:6px;
           padding:8px 18px; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.4; cursor:not-allowed; }
  button.danger { background:var(--bad); color:#fff; }
  button.mini { padding:3px 10px; font-size:12px; background:#21262d; color:var(--fg);
                border:1px solid var(--border); }
  button.mini:hover { border-color:var(--accent); }
  .warn { color:#d29922; font-size:12px; margin-top:10px; }
  #log { background:#0d1117; border:1px solid var(--border); border-radius:6px;
         padding:10px; height:150px; overflow:auto; font:12px/1.5 ui-monospace,monospace;
         white-space:pre-wrap; color:var(--dim); margin-top:12px; display:none; }
  progress { width:100%; height:8px; margin-top:10px; display:none; }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--border); }
  th { color:var(--dim); font-size:12px; font-weight:500; position:sticky; top:0;
       background:var(--card); }
  tr:hover td { background:#1c2128; }
  .stars { color:#e3b341; letter-spacing:1px; cursor:pointer; }
  .tag { display:inline-block; background:#21262d; border-radius:10px;
         padding:1px 8px; font-size:11px; margin:1px 2px; color:var(--dim); }
  .tag.bad { color:var(--bad); border:1px solid #f8514955; }
  .tag.good { color:var(--good); border:1px solid #3fb95055; }
  .meta { color:var(--dim); font-size:12px; margin-bottom:10px; }
  canvas { width:100%; height:220px; background:#0d1117;
           border:1px solid var(--border); border-radius:6px; }
  #chart-title { color:var(--dim); font-size:12px; margin-bottom:8px; }
  .mono { font-family:ui-monospace,monospace; }
  th.sort { cursor:pointer; user-select:none; }
  th.sort:hover { color:var(--fg); }
  th.sort .arr { color:var(--accent); font-size:10px; }
  tr.current td { background:#12261a; box-shadow:inset 3px 0 0 var(--good); }
  tr.current td:first-child { box-shadow:inset 3px 0 0 var(--good); }
  .cur-mark { color:var(--good); font-size:11px; margin-left:6px; }
  #profile-bar { margin-bottom:10px; display:flex; gap:6px; align-items:center;
                 flex-wrap:wrap; }
  .pf-label { color:var(--dim); font-size:12px; }
  button.mini.on { background:#1f6feb33; border-color:var(--accent); color:var(--accent); }
  #board { margin-bottom:10px; }
  #board-toggle { color:var(--dim); font-size:12px; cursor:pointer; user-select:none;
                  margin-bottom:6px; }
  #board-toggle:hover { color:var(--fg); }
  .board-group { margin:4px 0; font-size:12px; }
  .board-code { display:inline-block; min-width:56px; color:var(--accent); font-weight:600; }
  .board-item { display:inline-block; background:#21262d; border:1px solid var(--border);
                border-radius:6px; padding:1px 8px; margin:2px 4px 2px 0; cursor:pointer; }
  .board-item:hover { border-color:var(--accent); }
  .board-item b { color:var(--good); font-weight:600; }
  .fav { cursor:pointer; color:var(--dim); margin-right:5px; user-select:none; }
  .fav.on { color:#e3b341; }
  .sc-num { color:var(--fg); }
  #chart-sub { color:var(--dim); font-size:11px; margin:0 0 8px; min-height:14px; }
</style>
</head>
<body>
<h1>⚡ Clash SpeedBench <button class="mini" id="btn-quit" style="float:right">停止面板</button></h1>
<div class="sub">延迟 + 真实带宽 + IP 画像 + 综合评分 · 本地面板（127.0.0.1）<span id="cur-line"></span></div>

<div class="card">
  <div class="controls">
    <div><label>节点过滤（正则，留空=全部）</label>
      <input type="text" id="f-include" placeholder="例如 香港|HK"></div>
    <div><label>每轮 MB</label><input type="number" id="f-mb" value="30" min="1" max="95"></div>
    <div><label>轮数</label><input type="number" id="f-rounds" value="1" min="1" max="5"></div>
    <div><label>&nbsp;</label><label style="color:var(--fg)">
      <input type="checkbox" id="f-autoswitch"> 测完自动切到冠军</label></div>
    <div><label>&nbsp;</label><button id="btn-run">开始测速</button></div>
    <div><label>&nbsp;</label><button id="btn-cancel" class="danger" style="display:none">中断测速</button></div>
  </div>
  <div class="warn">⚠️ 测速期间 Mihomo 会临时切到 GLOBAL 模式，全网流量跟随被测节点；结束自动恢复。</div>
  <progress id="prog" max="100" value="0"></progress>
  <div id="log"></div>
</div>

<div class="card">
  <div class="meta" id="latest-meta">暂无测速记录</div>
  <div id="profile-bar">
    <span class="pf-label">评分 Profile</span>
    <button class="mini pf" data-p="all" title="后端综合评分（延迟+带宽+IP 画像加权）">综合推荐</button>
    <button class="mini pf" data-p="daily" title="0.5×延迟 + 0.3×抖动 + 0.2×带宽，日常浏览体验优先">⚡ 日常</button>
    <button class="mini pf" data-p="download" title="0.7×单线程带宽(300M封顶) + 0.3×多线程带宽(500M封顶)，下载优先">🚀 下载</button>
    <button class="mini pf" data-p="ipclean" title="按出口 IP 属性打分：无标记 100 / 移动 75 / 托管 50 / 代理 20">🧼 IP</button>
  </div>
  <div id="board" style="display:none">
    <div id="board-toggle"><span id="board-arrow">▸</span> 地区榜 · 各地区在当前 Profile 下的 Top 3（点击看趋势）</div>
    <div id="board-body" style="display:none"></div>
  </div>
  <div style="overflow:auto; max-height:480px;">
  <table>
    <thead><tr>
      <th>#</th>
      <th class="sort" data-k="name">节点 <span class="arr"></span></th>
      <th class="sort" data-k="latency_ms">延迟 <span class="arr"></span></th>
      <th class="sort" data-k="median_mbps">带宽 <span class="arr"></span></th>
      <th class="sort" data-k="score">评分 <span class="arr">▼</span></th>
      <th class="sort" data-k="ip">IP画像 <span class="arr"></span></th>
      <th>标签</th><th></th>
    </tr></thead>
    <tbody id="tbody"></tbody>
  </table>
  </div>
</div>

<div class="card">
  <div id="chart-title">历史带宽趋势（点击表格中任意节点查看）</div>
  <div id="chart-sub"></div>
  <canvas id="chart"></canvas>
</div>

<script>
let pollTimer = null;
let chartNode = null;

// 写操作令牌：由服务端每次启动随机生成并注入 <meta>，所有 POST 必须携带
const SB_TOKEN = document.querySelector('meta[name="sb-token"]').content;
async function post(url, body){
  const r = await fetch(url, {method:'POST',
    headers:{'X-SpeedBench-Token': SB_TOKEN, 'Content-Type':'application/json'},
    body: JSON.stringify(body||{})});
  return r.json();
}

function esc(s){ return (s??'').toString().replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

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

let latestData = null;
let sortKey = 'score', sortAsc = false;
let currentNode = '', currentGroup = '';

// localStorage 在某些隐私模式下会抛异常，包一层静默降级
function lsGet(k){ try{ return localStorage.getItem(k); }catch(e){ return null; } }
function lsSet(k,v){ try{ localStorage.setItem(k,v); }catch(e){} }

// —— 评分 Profile（纯前端换算，公式见 profileScore）——
// all=综合推荐(后端 score 原样) / daily=⚡日常 / download=🚀下载 / ipclean=🧼IP
const PROFILES = ['all','daily','download','ipclean'];
let currentProfile = lsGet('sb_profile');
if(!PROFILES.includes(currentProfile)) currentProfile = 'all';

// 收藏节点集：localStorage 持久化；只影响 ★ 展示和地区榜顶部速览，不干扰表格排序
let favs;
try{ favs = new Set(JSON.parse(lsGet('sb_favs')||'[]')); }
catch(e){ favs = new Set(); }

function clamp(v, lo, hi){ return Math.max(lo, Math.min(hi, v)); }

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

// IP 列按类型固定优先级排序：ISP/非托管 > 移动网络 > 机房托管 > 代理/VPN > 未知。
// 数值越大排越前（表格默认降序，与评分列习惯一致）；未识别的旧值按"未知"处理；
// 查询失败（无 ip 或 !ok）返回 null，由排序逻辑统一沉底
const KIND_RANK = {'ISP/非托管':4,'移动网络':3,'机房托管':2,'代理/VPN':1,'未知':0};
function sortVal(r, k){
  if(k==='ip'){ const ip=r.ip; return (ip&&ip.ok)?(KIND_RANK[normKind(ip)]??0):null; }
  if(k==='score') return profileScore(r);  // 评分列的排序键跟随当前 Profile
  return r[k];
}

function updateSortArrows(){
  document.querySelectorAll('th.sort').forEach(th=>{
    th.querySelector('.arr').textContent = th.dataset.k===sortKey ? (sortAsc?'▲':'▼') : '';
  });
}

function setSort(k){
  if(sortKey===k){ sortAsc=!sortAsc; } else { sortKey=k; sortAsc=(k==='name'||k==='latency_ms'); }
  updateSortArrows();
  renderTable();
}

// 切换评分 Profile：存 localStorage、高亮选中按钮、自动按新 Profile 分数降序
function setProfile(p){
  currentProfile = p;
  lsSet('sb_profile', p);
  document.querySelectorAll('#profile-bar .pf').forEach(b=>b.classList.toggle('on', b.dataset.p===p));
  sortKey='score'; sortAsc=false; updateSortArrows();
  renderTable(); renderBoard();
}

function renderTable(){
  const tbody = document.getElementById('tbody');
  if(!latestData || !latestData.results){ tbody.innerHTML=''; return; }
  const rows = latestData.results.slice();
  rows.sort((a,b)=>{
    const va=sortVal(a,sortKey), vb=sortVal(b,sortKey);
    if(va==null && vb==null) return 0;
    if(va==null) return 1;   // 不通的永远排最后
    if(vb==null) return -1;
    const cmp = (typeof va==='string') ? va.localeCompare(vb,'zh') : va-vb;
    return sortAsc ? cmp : -cmp;
  });
  tbody.innerHTML = rows.map((r,i)=>{
    const isCur = r.name===currentNode;
    const isFav = favs.has(r.name);
    const sc = profileScore(r);
    // 评分列 = 当前 Profile 分数（一位小数）+ 星标。星标选「始终显示后端 stars」：
    // stars 是后端综合评级的直观符号，Profile 切换只改数值与排序，不同步换算以免误导。
    return `<tr${isCur?' class="current"':''}>
    <td>${i+1}</td>
    <td><span class="fav${isFav?' on':''}" data-name="${esc(r.name)}" title="收藏/取消收藏">${isFav?'★':'☆'}</span>${esc(r.name)}${isCur?'<span class="cur-mark">✅ 当前</span>':''}</td>
    <td class="mono">${r.latency_ms??'-'}</td>
    <td class="mono">${r.median_mbps?r.median_mbps.toFixed(1):'-'}</td>
    <td class="stars" title="查看历史趋势" data-name="${esc(r.name)}"><span class="sc-num">${sc==null?'-':sc.toFixed(1)}</span> ${esc(r.stars||'')}</td>
    <td>${ipHtml(r.ip)}</td>
    <td>${tagHtml(r.tags)}</td>
    <td>${isCur?'<button class="mini" disabled>使用中</button>'
               :`<button class="mini sw" data-name="${esc(r.name)}">切换</button>`}</td>
  </tr>`;}).join('');
}

function renderCurrent(){
  document.getElementById('cur-line').textContent =
    currentNode ? ` · 当前：${currentGroup} = ${currentNode}` : '';
  renderTable();
}

// —— 地区榜 ——
// 地区分组启发式（优先级从高到低）：
// 1) IP 画像的 country_code（实测出口，最可靠）；
// 2) 节点名开头的国旗 emoji：地区指示符（U+1F1E6–U+1F1FF）两两一对，减偏移即得字母代码；
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

function toggleFav(name){
  if(favs.has(name)) favs.delete(name); else favs.add(name);
  lsSet('sb_favs', JSON.stringify([...favs]));
  renderTable(); renderBoard();
}

async function loadLatest(){
  const rec = await (await fetch('/api/latest')).json();
  if(rec.results){
    latestData = rec;
    document.getElementById('latest-meta').textContent =
      `上次测速：${rec.ts} · ${rec.results.length} 个节点 · ${rec.mb}MB×${rec.rounds}轮`;
    if(!chartNode && rec.results.length){ showChart(rec.results[0].name); }
  }
  renderTable(); renderBoard();
}

async function loadCurrent(){
  try{
    const r = await (await fetch('/api/current')).json();
    if(r.ok){ currentGroup=r.group; currentNode=r.now; }
  }catch(e){}
  renderCurrent();
}

async function startRun(){
  const body = {
    include: document.getElementById('f-include').value,
    mb: +document.getElementById('f-mb').value,
    rounds: +document.getElementById('f-rounds').value,
    auto_switch: document.getElementById('f-autoswitch').checked,
  };
  const r = await post('/api/run', body);
  if(!r.ok){ alert(r.msg); return; }
  document.getElementById('btn-run').disabled = true;
  document.getElementById('btn-cancel').style.display = '';
  document.getElementById('log').style.display='block';
  document.getElementById('prog').style.display='block';
  pollTimer = setInterval(pollStatus, 1200);
  pollStatus();
}

async function pollStatus(){
  const s = await (await fetch('/api/run/status')).json();
  const log = document.getElementById('log');
  log.textContent = s.lines.join('\n');
  log.scrollTop = log.scrollHeight;
  let cur=0, total=0;
  for(const ln of s.lines){
    const m = ln.match(/\[\s*(\d+)\/(\d+)\]/);
    if(m){ cur=+m[1]; total=+m[2]; }
  }
  if(total) document.getElementById('prog').value = cur/total*100;
  if(!s.running){
    clearInterval(pollTimer);
    document.getElementById('btn-run').disabled = false;
    document.getElementById('btn-cancel').style.display = 'none';
    loadLatest(); loadHistory(); loadCurrent();
  }
}

async function switchNode(name){
  const r = await post('/api/switch', {name});
  if(r.ok){
    currentNode = name;
    if(r.group) currentGroup = r.group;
    renderCurrent();
  }else{
    alert(r.msg);
  }
}

async function cancelRun(){
  if(!confirm('中断当前测速？会向测速进程发送中断信号，恢复 Clash 配置后停止。')) return;
  const r = await post('/api/run/cancel');
  if(!r.ok){ alert(r.msg); return; }
  pollStatus();
}

async function quitPanel(){
  if(!confirm('停止 SpeedBench 面板？若测速仍在进行，会先中断测速并恢复 Clash 配置，然后停止面板。')) return;
  try{ await post('/api/quit'); }catch(e){}
  document.body.innerHTML='<div style="text-align:center;padding:80px;color:#8b949e">面板已停止，可以关闭此标签页。<br>下次双击 Clash SpeedBench 图标重新启动。</div>';
}

let histData = [];
let chartSeries = null;  // {name, pts:[{ts,v}], ipNote}；null 或 name 与 chartNode 不符时回退 histData
async function loadHistory(){ histData = await (await fetch('/api/history')).json(); drawChart(); }

function showChart(name){ chartNode = name; fetchNodeSeries(name); drawChart(); }

// 单节点图表数据源：优先 /api/node 的 30 天序列（含 IP 变化时间线），失败回退 histData
async function fetchNodeSeries(name){
  try{
    const d = await (await fetch('/api/node?name='+encodeURIComponent(name)+'&days=30')).json();
    if(chartNode!==name) return;  // 等待期间用户已改选别的节点，丢弃过期响应
    if(d.series && d.series.length){
      chartSeries = {
        name,
        pts: d.series.filter(s=>s.median_mbps!=null).map(s=>({ts:s.ts.slice(5,16), v:s.median_mbps})),
        ipNote: ipChangeNote(d.ip_changes),
      };
      drawChart();
    }
  }catch(e){ /* 静默回退 histData，drawChart 已画过兜底版 */ }
}

// ip_changes 是「相邻不变则合并」的变化点时间线：首条是初始 IP，之后每条算一次变化
function ipChangeNote(changes){
  if(!changes || changes.length<2) return '';
  const last = changes[changes.length-1], prev = changes[changes.length-2];
  return `出口 IP 曾变化 ${changes.length-1} 次（最近：${prev.exit_ip||'?'} → ${last.exit_ip||'?'} @ ${last.ts.slice(0,16)}）`;
}

function drawChart(){
  const cv = document.getElementById('chart');
  const dpr = window.devicePixelRatio||1;
  const W = cv.clientWidth*dpr, H = cv.clientHeight*dpr;
  cv.width=W; cv.height=H;
  const ctx = cv.getContext('2d');
  ctx.clearRect(0,0,W,H);
  document.getElementById('chart-title').textContent =
    chartNode ? `历史带宽趋势：${chartNode}` : '历史带宽趋势（点击表格中任意节点查看）';
  // 数据源：/api/node 的 30 天序列优先；取不到（或序列不属于当前节点）回退 /api/history
  const useSeries = chartSeries && chartSeries.name===chartNode && chartSeries.pts.length;
  document.getElementById('chart-sub').textContent = useSeries ? (chartSeries.ipNote||'') : '';
  const pts = [];
  if(useSeries){
    pts.push(...chartSeries.pts);
  }else if(chartNode && histData.length){
    for(const rec of histData){
      const r = rec.results.find(x=>x.name===chartNode);
      if(r && r.median_mbps!=null) pts.push({ts:rec.ts.slice(5,16), v:r.median_mbps});
    }
  }
  if(!chartNode) return;
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

// 事件绑定：节点名一律走 dataset（HTML 属性经 esc 转义），绝不拼接进 JS 源码
document.getElementById('tbody').addEventListener('click', e=>{
  const fv = e.target.closest('.fav');
  if(fv && fv.dataset.name!=null){ toggleFav(fv.dataset.name); return; }
  const btn = e.target.closest('button.sw');
  if(btn){ switchNode(btn.dataset.name); return; }
  const cell = e.target.closest('td.stars');
  if(cell && cell.dataset.name!=null) showChart(cell.dataset.name);
});
document.querySelectorAll('th.sort').forEach(th=>
  th.addEventListener('click', ()=>setSort(th.dataset.k)));
// Profile 按钮：初始化选中态（localStorage 恢复）+ 点击切换
document.querySelectorAll('#profile-bar .pf').forEach(b=>{
  b.classList.toggle('on', b.dataset.p===currentProfile);
  b.addEventListener('click', ()=>setProfile(b.dataset.p));
});
// 地区榜：标题点击折叠/展开（默认折叠），条目点击看该节点趋势
document.getElementById('board-toggle').addEventListener('click', ()=>{
  const body = document.getElementById('board-body');
  const open = body.style.display==='none';
  body.style.display = open?'':'none';
  document.getElementById('board-arrow').textContent = open?'▾':'▸';
});
document.getElementById('board-body').addEventListener('click', e=>{
  const it = e.target.closest('.board-item');
  if(it && it.dataset.name!=null) showChart(it.dataset.name);
});
document.getElementById('btn-run').addEventListener('click', startRun);
document.getElementById('btn-cancel').addEventListener('click', cancelRun);
document.getElementById('btn-quit').addEventListener('click', quitPanel);

loadLatest(); loadHistory(); loadCurrent();
window.addEventListener('resize', drawChart);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "SpeedBenchWeb/0.1"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            page = PAGE.replace("__SB_TOKEN__", WEB_TOKEN)
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/latest":
            self._json(latest_record())
        elif path == "/api/current":
            self._json(get_current())
        elif path == "/api/history":
            self._json(slim_history())
        elif path == "/api/node":
            # 单节点详情：近 N 天测速序列 + 出口 IP 变化时间线（SQL 参数化防注入）
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = qs.get("name", [""])[0]
            if not name:
                self._json({"ok": False, "msg": "缺少 name 参数"}, 400)
                return
            try:
                days = max(1, min(int(qs.get("days", ["30"])[0]), 3650))
            except (ValueError, TypeError):
                days = 30
            sync_db()
            self._json({
                "series": speedbench_db.node_series(db_path(), name, days=days),
                "ip_changes": speedbench_db.ip_changes(db_path(), name),
            })
        elif path == "/api/run/status":
            with STATE_LOCK:
                self._json({
                    "running": STATE["running"],
                    "lines": STATE["lines"][-60:],
                    "exit_code": STATE["exit_code"],
                })
        else:
            self._json({"ok": False, "msg": "not found"}, 404)

    def _check_post(self) -> bool:
        """POST 写操作防护：令牌 + Host + Origin 三重校验，任一不符返回 403。

        面板只绑 127.0.0.1，但其他网页仍可跨站向本机端口发 POST（CSRF /
        DNS rebinding），因此所有写操作必须携带页面注入的随机令牌。
        """
        port = self.server.server_port
        token = self.headers.get("X-SpeedBench-Token") or ""
        if not secrets.compare_digest(token, WEB_TOKEN):
            self._json({"ok": False, "msg": "Forbidden: 令牌无效"}, 403)
            return False
        if self.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._json({"ok": False, "msg": "Forbidden: Host 不允许"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            self._json({"ok": False, "msg": "Forbidden: Origin 不允许"}, 403)
            return False
        return True

    def do_POST(self) -> None:
        if not self._check_post():
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/run":
            with STATE_LOCK:
                busy = STATE["running"]
            if busy:
                self._json({"ok": False, "msg": "已有测速任务进行中"}, 409)
                return
            params = self._read_body()
            threading.Thread(target=run_benchmark, args=(params,), daemon=True).start()
            self._json({"ok": True})
        elif path == "/api/switch":
            name = str(self._read_body().get("name", ""))
            if not name:
                self._json({"ok": False, "msg": "缺少节点名"}, 400)
                return
            self._json(do_switch(name))
        elif path == "/api/run/cancel":
            self._json(cancel_benchmark())
        elif path == "/api/quit":
            with STATE_LOCK:
                busy = STATE["running"]
            if busy:
                cancel_benchmark()  # 先中断测速（SIGINT 恢复 Clash 配置），再停面板
            msg = "面板已停止" + ("，已先中断进行中的测速" if busy else "")
            self._json({"ok": True, "msg": msg})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self._json({"ok": False, "msg": "not found"}, 404)


def main() -> int:
    parser = argparse.ArgumentParser(description="Clash SpeedBench 本地 Web 面板")
    parser.add_argument("--port", type=int, default=8950)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    url = f"http://127.0.0.1:{args.port}"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        n = sync_db()  # 启动时先把 jsonl 历史增量入库（幂等）
        if n:
            print(f"历史库：新导入 {n} 轮测速记录 → {db_path().name}")
    except Exception as e:
        print(f"历史库导入失败（不影响面板使用）: {e}")
    print(f"Clash SpeedBench 面板: {url}")
    print("Ctrl+C 停止。测速期间 Mihomo 会临时切到 GLOBAL 模式，结束自动恢复。")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
