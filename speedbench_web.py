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

SCRIPT = HERE / "clash_speedbench.py"
# 数据目录：默认脚本同级；打包成 .app 时由启动器用 SPEEDBENCH_HOME 指到
# ~/Library/Application Support/ClashSpeedBench，避免污染应用包。
DATA_HOME = Path(os.environ.get("SPEEDBENCH_HOME", str(HERE)))
HISTORY = DATA_HOME / "speedbench-history.jsonl"

STATE = {
    "running": False,
    "lines": [],
    "started": None,
    "exit_code": None,
}
STATE_LOCK = threading.Lock()

MAX_LINES = 500


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
    records = read_history()
    return records[-1] if records else {}


def slim_history() -> list:
    """Trend data: per run, per node, only the fields the chart needs."""
    out = []
    for rec in read_history():
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
</style>
</head>
<body>
<h1>⚡ Clash SpeedBench <button class="mini" style="float:right" onclick="quitPanel()">停止面板</button></h1>
<div class="sub">延迟 + 真实带宽 + IP 纯净度 + 综合评分 · 本地面板（127.0.0.1）<span id="cur-line"></span></div>

<div class="card">
  <div class="controls">
    <div><label>节点过滤（正则，留空=全部）</label>
      <input type="text" id="f-include" placeholder="例如 香港|HK"></div>
    <div><label>每轮 MB</label><input type="number" id="f-mb" value="30" min="1" max="95"></div>
    <div><label>轮数</label><input type="number" id="f-rounds" value="1" min="1" max="5"></div>
    <div><label>&nbsp;</label><label style="color:var(--fg)">
      <input type="checkbox" id="f-autoswitch"> 测完自动切到冠军</label></div>
    <div><label>&nbsp;</label><button id="btn-run" onclick="startRun()">开始测速</button></div>
  </div>
  <div class="warn">⚠️ 测速期间 Mihomo 会临时切到 GLOBAL 模式，全网流量跟随被测节点；结束自动恢复。</div>
  <progress id="prog" max="100" value="0"></progress>
  <div id="log"></div>
</div>

<div class="card">
  <div class="meta" id="latest-meta">暂无测速记录</div>
  <div style="overflow:auto; max-height:480px;">
  <table>
    <thead><tr>
      <th>#</th>
      <th class="sort" data-k="name" onclick="setSort('name')">节点 <span class="arr"></span></th>
      <th class="sort" data-k="latency_ms" onclick="setSort('latency_ms')">延迟 <span class="arr"></span></th>
      <th class="sort" data-k="median_mbps" onclick="setSort('median_mbps')">带宽 <span class="arr"></span></th>
      <th class="sort" data-k="score" onclick="setSort('score')">评分 <span class="arr">▼</span></th>
      <th class="sort" data-k="ip" onclick="setSort('ip')">IP画像 <span class="arr"></span></th>
      <th>标签</th><th></th>
    </tr></thead>
    <tbody id="tbody"></tbody>
  </table>
  </div>
</div>

<div class="card">
  <div id="chart-title">历史带宽趋势（点击表格中任意节点查看）</div>
  <canvas id="chart"></canvas>
</div>

<script>
let pollTimer = null;
let chartNode = null;

function esc(s){ return (s??'').toString().replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function tagHtml(tags){
  if(!tags) return '-';
  return tags.split(',').map(t=>{
    let cls='tag';
    if(/不通|龟速|脏IP|高风险|高延迟/.test(t)) cls+=' bad';
    if(/低延迟|高带宽|住宅/.test(t)) cls+=' good';
    return `<span class="${cls}">${esc(t)}</span>`;
  }).join('');
}

function ipHtml(ip){
  if(!ip || !ip.ok) return '-';
  return `${esc(ip.country_code||ip.country)}·${esc(ip.kind)}·风险${esc(ip.risk)}`;
}

let latestData = null;
let sortKey = 'score', sortAsc = false;
let currentNode = '', currentGroup = '';

function sortVal(r, k){
  if(k==='ip'){ const ip=r.ip; return (ip&&ip.ok)?(100-(+ip.risk||0)):null; }
  return r[k];
}

function setSort(k){
  if(sortKey===k){ sortAsc=!sortAsc; } else { sortKey=k; sortAsc=(k==='name'||k==='latency_ms'); }
  document.querySelectorAll('th.sort').forEach(th=>{
    th.querySelector('.arr').textContent = th.dataset.k===sortKey ? (sortAsc?'▲':'▼') : '';
  });
  renderTable();
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
    return `<tr${isCur?' class="current"':''}>
    <td>${i+1}</td>
    <td>${esc(r.name)}${isCur?'<span class="cur-mark">✅ 当前</span>':''}</td>
    <td class="mono">${r.latency_ms??'-'}</td>
    <td class="mono">${r.median_mbps?r.median_mbps.toFixed(1):'-'}</td>
    <td class="stars" title="查看历史趋势" onclick="showChart('${esc(r.name)}')">${esc(r.stars)}</td>
    <td>${ipHtml(r.ip)}</td>
    <td>${tagHtml(r.tags)}</td>
    <td>${isCur?'<button class="mini" disabled>使用中</button>'
               :`<button class="mini" onclick="switchNode('${esc(r.name)}')">切换</button>`}</td>
  </tr>`;}).join('');
}

function renderCurrent(){
  document.getElementById('cur-line').textContent =
    currentNode ? ` · 当前：${currentGroup} = ${currentNode}` : '';
  renderTable();
}

async function loadLatest(){
  const rec = await (await fetch('/api/latest')).json();
  if(rec.results){
    latestData = rec;
    document.getElementById('latest-meta').textContent =
      `上次测速：${rec.ts} · ${rec.results.length} 个节点 · ${rec.mb}MB×${rec.rounds}轮`;
    if(!chartNode && rec.results.length){ chartNode = rec.results[0].name; drawChart(); }
  }
  renderTable();
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
  const r = await (await fetch('/api/run',{method:'POST',body:JSON.stringify(body)})).json();
  if(!r.ok){ alert(r.msg); return; }
  document.getElementById('btn-run').disabled = true;
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
    loadLatest(); loadHistory(); loadCurrent();
  }
}

async function switchNode(name){
  const r = await (await fetch('/api/switch',{method:'POST',body:JSON.stringify({name})})).json();
  if(r.ok){
    currentNode = name;
    if(r.group) currentGroup = r.group;
    renderCurrent();
  }else{
    alert(r.msg);
  }
}

async function quitPanel(){
  if(!confirm('停止 SpeedBench 面板？（正在进行的测速会中断并自动恢复 Clash 配置）')) return;
  try{ await fetch('/api/quit',{method:'POST'}); }catch(e){}
  document.body.innerHTML='<div style="text-align:center;padding:80px;color:#8b949e">面板已停止，可以关闭此标签页。<br>下次双击 Clash SpeedBench 图标重新启动。</div>';
}

let histData = [];
async function loadHistory(){ histData = await (await fetch('/api/history')).json(); drawChart(); }

function showChart(name){ chartNode = name; drawChart(); }

function drawChart(){
  const cv = document.getElementById('chart');
  const dpr = window.devicePixelRatio||1;
  const W = cv.clientWidth*dpr, H = cv.clientHeight*dpr;
  cv.width=W; cv.height=H;
  const ctx = cv.getContext('2d');
  ctx.clearRect(0,0,W,H);
  document.getElementById('chart-title').textContent =
    chartNode ? `历史带宽趋势：${chartNode}` : '历史带宽趋势（点击表格中任意节点查看）';
  if(!chartNode || !histData.length) return;
  const pts = [];
  for(const rec of histData){
    const r = rec.results.find(x=>x.name===chartNode);
    if(r && r.median_mbps!=null) pts.push({ts:rec.ts.slice(5,16), v:r.median_mbps});
  }
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
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/latest":
            self._json(latest_record())
        elif path == "/api/current":
            self._json(get_current())
        elif path == "/api/history":
            self._json(slim_history())
        elif path == "/api/run/status":
            with STATE_LOCK:
                self._json({
                    "running": STATE["running"],
                    "lines": STATE["lines"][-60:],
                    "exit_code": STATE["exit_code"],
                })
        else:
            self._json({"ok": False, "msg": "not found"}, 404)

    def do_POST(self) -> None:
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
        elif path == "/api/quit":
            self._json({"ok": True, "msg": "面板已停止"})
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
