#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clash SpeedBench 本地 Web 面板
- Zero-dependency: stdlib http.server only; front-end served from web/ static files
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

# 前端静态文件目录与分发白名单：URL 路径 → (磁盘文件名, MIME)。
# 只认这三个文件、不做任何路径拼接，天然免疫 ".." 穿越；其余一律 404。
WEB_DIR = HERE / "web"
STATIC_FILES = {
    "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}


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

    def _serve_index(self) -> None:
        """读 web/index.html 并注入本次启动的随机令牌；缺失说明安装/构建不完整。"""
        try:
            html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        except OSError:
            self._send(503,
                       "web/index.html 缺失：前端文件未安装，请重新安装或重新构建应用。"
                       .encode("utf-8"),
                       "text/plain; charset=utf-8")
            return
        self._send(200, html.replace("__SB_TOKEN__", WEB_TOKEN).encode("utf-8"),
                   "text/html; charset=utf-8")

    def _serve_static(self, path: str) -> None:
        """按 STATIC_FILES 白名单分发静态文件；白名单内但磁盘缺失同样 404。"""
        fname, ctype = STATIC_FILES[path]
        try:
            body = (WEB_DIR / fname).read_bytes()
        except OSError:
            self._json({"ok": False, "msg": "not found"}, 404)
            return
        self._send(200, body, ctype)

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_index()
        elif path in STATIC_FILES:
            self._serve_static(path)
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
