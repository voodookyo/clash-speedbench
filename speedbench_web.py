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
import speedbench_ip_intel  # noqa: E402
import speedbench_leak  # noqa: E402
import speedbench_tray  # noqa: E402

SCRIPT = HERE / "clash_speedbench.py"
# 数据目录：默认脚本同级；打包成 .app 时由启动器用 SPEEDBENCH_HOME 指到
# ~/Library/Application Support/ClashSpeedBench，避免污染应用包。
DATA_HOME = Path(os.environ.get("SPEEDBENCH_HOME", str(HERE)))
HISTORY = DATA_HOME / "speedbench-history.jsonl"
# 「停止测速」哨兵文件：面板无控制台（pythonw），CTRL_BREAK_EVENT 无处可投，
# 改写哨兵文件，测速子进程在节点/轮次间隙发现后走 KeyboardInterrupt 优雅中断。
CANCEL_FILE = DATA_HOME / "cancel-request"

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

# 令牌同时写入数据目录（0600 仅本人可读），供本机受信脚本（SwiftBar 菜单栏
# 插件等）调用写操作 API（如 /api/quit）。每次启动覆盖，面板停掉后自然失效。
TOKEN_FILE = DATA_HOME / "web-token"

# Optional IP Intelligence credentials supplied from the Web UI.  These
# values intentionally live only in this process.  A key present in the
# environment remains the default until the user explicitly clears it with
# an empty value through the settings endpoint.  No value from this mapping
# is ever returned by an API or written to STATE/history/SQLite.
_IP_INTEL_SETTING_ENV = {
    "ipinfo_token": "SPEEDBENCH_IPINFO_TOKEN",
    "ipqs_key": "SPEEDBENCH_IPQS_KEY",
    "scamalytics_username": "SPEEDBENCH_SCAMALYTICS_USERNAME",
    "scamalytics_key": "SPEEDBENCH_SCAMALYTICS_KEY",
    "scamalytics_region": "SPEEDBENCH_SCAMALYTICS_REGION",
}
_IP_INTEL_SETTING_LIMITS = {
    "ipinfo_token": 512,
    "ipqs_key": 512,
    "scamalytics_username": 128,
    "scamalytics_key": 512,
    "scamalytics_region": 2,
}
_IP_INTEL_OVERRIDES = {}
_IP_INTEL_SETTINGS_LOCK = threading.RLock()


def _provider_config() -> speedbench_ip_intel.ProviderConfig:
    """Return an env + in-memory credential snapshot without exposing it."""
    # Keep provider feature flags sourced from the same environment parser as
    # the CLI.  Credentials may be overridden in memory below, but the
    # explicit ip-api opt-out remains an environment-level switch.
    env_config = speedbench_ip_intel.load_provider_config()
    values = {}
    with _IP_INTEL_SETTINGS_LOCK:
        for field, env_name in _IP_INTEL_SETTING_ENV.items():
            if field in _IP_INTEL_OVERRIDES:
                value = _IP_INTEL_OVERRIDES[field]
            else:
                value = os.environ.get(env_name)
            if isinstance(value, str):
                value = value.strip() or None
            values[field] = value
    region = values.get("scamalytics_region")
    if region:
        region = str(region).lower()
    return speedbench_ip_intel.ProviderConfig(
        ipinfo_token=values.get("ipinfo_token"),
        ipqs_key=values.get("ipqs_key"),
        scamalytics_username=values.get("scamalytics_username"),
        scamalytics_key=values.get("scamalytics_key"),
        scamalytics_region=region,
        ip_api_enabled=env_config.ip_api_enabled,
    )


def _provider_status_payload() -> dict:
    """Configuration-only status; credentials never cross this boundary."""
    try:
        config = _provider_config()
        providers = speedbench_ip_intel.make_default_providers(config=config)
        statuses = speedbench_ip_intel.provider_status_snapshot(providers)
    except Exception:
        statuses = {
            "ip-api": "error", "ipinfo": "error", "ipqs": "error",
            "scamalytics": "error",
        }
    # If a recent run already queried a provider, surface its safe runtime
    # state (cache_hit/rate_limited/quota_unavailable) alongside the current
    # configuration state.  Only status strings are copied from history.
    observed = {}
    try:
        recent = latest_record()
        for item in (recent.get("results", []) if isinstance(recent, dict) else []):
            for family in (item.get("intel_v4"), item.get("intel_v6")):
                if not isinstance(family, dict):
                    continue
                values = family.get("provider_status")
                if isinstance(values, dict):
                    observed.update({str(k): str(v) for k, v in values.items()
                                     if str(v) in speedbench_ip_intel.PROVIDER_STATUSES})
    except Exception:
        observed = {}
    result = {}
    for name, status in statuses.items():
        # ``configured`` is deliberately a Boolean rather than a credential
        # hint.  The status itself is one of the documented safe states.
        # A current explicit disable must win over an older cache_hit from a
        # previous run; the opt-out is not revoked by history.
        observed_status = observed.get(name)
        effective_status = (
            status
            if status == "disabled" or observed_status == "disabled"
            else observed_status or status
        )
        result[name] = {
            "configured": status == "ok",
            "status": effective_status,
            "cache": "available",
        }
    return {
        "ok": True,
        "providers": result,
        "cache": {"available": True, "policy": "ip-api:7d,risk:24h"},
    }


def _provider_env_snapshot() -> dict:
    """Copy subprocess environment and inject only the current credentials.

    The command line and STATE log remain credential-free.  Explicitly
    removing an inherited variable is important when the user cleared a
    value in the in-memory settings form.
    """
    env = dict(os.environ)
    config = _provider_config()
    values = {
        "SPEEDBENCH_IPINFO_TOKEN": config.ipinfo_token,
        "SPEEDBENCH_IPQS_KEY": config.ipqs_key,
        "SPEEDBENCH_SCAMALYTICS_USERNAME": config.scamalytics_username,
        "SPEEDBENCH_SCAMALYTICS_KEY": config.scamalytics_key,
        "SPEEDBENCH_SCAMALYTICS_REGION": config.scamalytics_region,
    }
    for name, value in values.items():
        env.pop(name, None)
        if value:
            env[name] = value
    return env


def _redact_runtime_text(value: object) -> str:
    """Redact in-memory provider credentials before a line reaches STATE."""
    text = str(value)
    try:
        config = _provider_config()
        for secret in (config.ipinfo_token, config.ipqs_key,
                       config.scamalytics_username, config.scamalytics_key):
            if secret:
                text = text.replace(str(secret), "[REDACTED]")
    except Exception:
        pass
    # Also cover common credential query parameter forms if a future provider
    # emits a malformed error before its own sanitizer runs.
    text = re.sub(r"(?i)([?&](?:key|token|api[_-]?key|authorization)=)[^&\s]+",
                  r"\1[REDACTED]", text)
    return text


def _set_ip_intel_settings(payload: object) -> tuple:
    """Validate and update memory-only settings.

    Returns ``(ok, message)``.  Messages contain field names/status only and
    never reflect the submitted value, which keeps API errors safe to display.
    """
    if not isinstance(payload, dict):
        return False, "请求格式无效"
    unknown = [key for key in payload if key not in _IP_INTEL_SETTING_ENV]
    if unknown:
        return False, "存在不支持的设置项"
    updates = {}
    for field, value in payload.items():
        if value is None:
            updates[field] = None
            continue
        if not isinstance(value, str):
            return False, "设置值必须是文本"
        value = value.strip()
        if len(value) > _IP_INTEL_SETTING_LIMITS[field]:
            return False, "设置值过长"
        if field == "scamalytics_region" and value and value.lower() not in {"us", "eu"}:
            return False, "Scamalytics 区域必须是 us 或 eu"
        updates[field] = value.lower() if field == "scamalytics_region" and value else (value or None)
    with _IP_INTEL_SETTINGS_LOCK:
        _IP_INTEL_OVERRIDES.update(updates)
    return True, "设置已更新（仅驻留内存）"


def write_token_file() -> None:
    try:
        TOKEN_FILE.write_text(WEB_TOKEN, encoding="utf-8")
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass  # 写不进去只是菜单栏无法停止面板，不影响面板本身

# jsonl → DB 的同步策略：读取前惰性增量同步。每次读 API 先比对 jsonl mtime，
# 有变化才 import_jsonl（导入本身按 ts 去重，幂等），面板读到的永远是最新数据，
# mtime 不变时代价只是一次 stat；启动时与 /api/run 结束后再各显式同步一次，
# 只为让导入问题尽早暴露。
_DB_SYNC_LOCK = threading.Lock()
_DB_SYNCED = {}  # str(db_path) -> 已同步的 jsonl mtime

# DB 里 provider 为空的行在 API 层展示成这个名字；/api/subscription 回传它时
# 也按 provider='' 查询
UNKNOWN_PROVIDER = "(未知订阅)"


def _days_param(qs: dict, default: int = 30) -> int:
    """days 查询参数解析：默认 30、钳到 [1, 3650]，非数字回退默认。"""
    try:
        return max(1, min(int(qs.get("days", [str(default)])[0]), 3650))
    except (ValueError, TypeError):
        return default


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
                    "provider": r.get("provider") or "",
                    "median_mbps": r.get("median_mbps"),
                    "latency_ms": r.get("latency_ms"),
                    "score": r.get("score"),
                }
                for r in rec.get("results", [])
            ],
        })
    return out


def run_benchmark(params: dict) -> None:
    # -u：子进程 stdout 走管道时默认块缓冲，进度行会堵在缓冲区里，
    # 面板看不到实时进度；无缓冲模式让每行立即到达。
    cmd = [sys.executable, "-u", str(SCRIPT), "--yes", "--history", str(HISTORY)]
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
        # Windows：面板无控制台（pythonw 启动），测速子进程同样没有可依附的
        # 控制台——CTRL_BREAK_EVENT 无处可投，取消改走哨兵文件（见
        # cancel_benchmark / CANCEL_FILE）。CREATE_NO_WINDOW 防止子进程弹窗。
        # stdin=DEVNULL：pythonw 的 stdin 句柄无效，子进程继承会出问题；
        # 且面板场景不该有 getpass 之类的控制台交互。
        popen_kwargs: dict = {"stdin": subprocess.DEVNULL}
        if sys.platform == "win32":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if flags:
                popen_kwargs["creationflags"] = flags
        # 把哨兵文件路径传给子进程（clash_speedbench.py 的 cancel_requested）
        env = _provider_env_snapshot()
        env["SPEEDBENCH_CANCEL_FILE"] = str(CANCEL_FILE)
        proc = subprocess.Popen(
            cmd, cwd=str(DATA_HOME), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            **popen_kwargs,
        )
        with STATE_LOCK:
            STATE["proc"] = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            line = _redact_runtime_text(line.rstrip("\n"))
            with STATE_LOCK:
                STATE["lines"].append(line)
                if len(STATE["lines"]) > MAX_LINES:
                    STATE["lines"] = STATE["lines"][-MAX_LINES:]
        STATE["exit_code"] = proc.wait()
    except Exception as e:
        with STATE_LOCK:
            STATE["lines"].append(f"!! 启动测速失败: {_redact_runtime_text(e)}")
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
                STATE["lines"].append(f"!! 历史入库失败: {_redact_runtime_text(e)}")


def cancel_benchmark() -> dict:
    """中断正在运行的测速子进程。

    POSIX 发 SIGINT；Windows 写哨兵文件（面板无控制台后 CTRL_BREAK_EVENT
    无处可投；测速子进程在节点/轮次间隙检查 cancel_requested，发现后转
    KeyboardInterrupt）——两者都走 clash_speedbench.py 的 finally 恢复
    Clash 策略组/模式。Windows 的 terminate 是 TerminateProcess，不跑
    finally，所以只作兜底：最多等 5 秒，未退出再 terminate（再兜底 kill）。
    """
    with STATE_LOCK:
        proc = STATE.get("proc")
        running = STATE["running"]
    if not running or proc is None or proc.poll() is not None:
        return {"ok": False, "msg": "当前没有正在进行的测速"}
    try:
        if sys.platform == "win32":
            CANCEL_FILE.write_text(str(int(time.time())), encoding="utf-8")
        else:
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


def _basic_ip_lookup(ip: str) -> dict:
    """Basic, non-reputation lookup used only for China/Unicom leak hints.

    Tests replace ``LEAK_BASIC_LOOKUP`` with a fixture.  This fallback uses
    the existing no-key ip-api provider and never calls IPinfo/IPQS/
    Scamalytics; leak candidates therefore cannot consume paid reputation
    quota.
    """
    try:
        provider = speedbench_ip_intel.IpApiProvider(
            enabled=_provider_config().ip_api_enabled
        )
        result = provider.query(ip)
        if result.ok:
            return dict(result.normalized)
    except Exception:
        pass
    return {}


# Injectable for tests and for installations that provide their own basic
# lookup.  It is intentionally not an IP reputation provider.
LEAK_BASIC_LOOKUP = _basic_ip_lookup


def _evaluate_leak_payload(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {"ok": False, "status": "unknown", "status_text": "无法确认",
                "msg": "请求格式无效"}
    # Explicit allow-list: provider fields, API keys and arbitrary URLs are
    # not accepted by this endpoint and cannot accidentally reach a vendor.
    candidates = payload.get("candidates", payload.get("ice_candidates", []))
    if not isinstance(candidates, (list, tuple, str, dict)):
        candidates = []
    evaluation = speedbench_leak.evaluate_webrtc(
        candidates,
        exit_ipv4=payload.get("exit_ipv4", payload.get("ipv4")),
        exit_ipv6=payload.get("exit_ipv6", payload.get("ipv6")),
        basic_lookup=LEAK_BASIC_LOOKUP,
        collection_complete=bool(payload.get("collection_complete", True)),
        collection_error=payload.get("collection_error"),
        policy_blocked=bool(payload.get("policy_blocked", False)),
    )
    return evaluation.to_dict()


def _save_leak_audit(audit: dict) -> dict:
    """Best-effort adapter for the additive DB API.

    Older databases/modules have no leak table yet.  The UI should continue
    to work and report ``available=False`` instead of failing the audit.
    """
    fn = getattr(speedbench_db, "insert_leak_audit", None)
    if not callable(fn):
        return {"available": False, "saved": False, "status": "unavailable"}
    try:
        value = fn(db_path(), audit)
        return {"available": True, "saved": True, "status": "ok",
                "id": value if isinstance(value, (int, str)) else None}
    except Exception:
        return {"available": True, "saved": False, "status": "error"}


def _load_leak_audits(limit: int = 20) -> dict:
    fn = getattr(speedbench_db, "leak_audits", None)
    if not callable(fn):
        return {"ok": True, "available": False, "audits": []}
    try:
        rows = fn(db_path(), max(1, min(int(limit), 100)))
        if not isinstance(rows, list):
            rows = list(rows or [])
        return {"ok": True, "available": True, "audits": rows}
    except Exception:
        return {"ok": True, "available": True, "audits": [], "status": "error"}


def _load_ip_reputation_changes(name: str = "", node_key: str = "") -> list:
    """Load the additive reputation timeline without requiring a new DB API.

    The web panel is also used with databases/modules created by older
    SpeedBench versions.  Keep this adapter deliberately best-effort: the
    richer timeline is optional and a missing table/function must never make
    the existing node endpoint fail.
    """
    fn = getattr(speedbench_db, "ip_reputation_changes", None)
    if not callable(fn):
        return []
    try:
        rows = fn(db_path(), name=name, node_key=node_key)
    except TypeError:
        # Compatibility with an intermediate implementation that accepted
        # positional arguments only (or the old name-only signature).
        try:
            rows = fn(db_path(), name, node_key)
        except TypeError:
            try:
                rows = fn(db_path(), name)
            except Exception:
                return []
        except Exception:
            return []
    except Exception:
        return []
    if not isinstance(rows, list):
        try:
            rows = list(rows or [])
        except Exception:
            return []
    return rows


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
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return {}
        # Avoid allowing a malformed client to make the handler read an
        # unbounded body.  The endpoint payloads are all tiny JSON objects.
        if length < 0 or length > 1024 * 1024:
            return {}
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _drain_body(self) -> None:
        """丢弃尚未读取的请求体（上限与 _read_body 一致）。

        面板响应后即关闭连接；若拒绝请求时 body 仍残留在 socket 缓冲区，
        Windows 关连接会发 RST，客户端可能收到 WinError 10053 而不是
        正常的 4xx 响应（CI windows-latest 实测）。拒绝前先把 body 读尽，
        关闭时就是干净的 FIN。超限/畸形的声明直接交给关连接处理。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return
        if length <= 0 or length > 1024 * 1024:
            return
        try:
            self.rfile.read(length)
        except (OSError, ValueError):
            pass

    def _reject(self, msg: str, code: int = 403) -> bool:
        """拒绝请求的公共出口：先丢弃 body 再响应，返回 False 供 gate 使用。"""
        self._drain_body()
        self._json({"ok": False, "msg": msg}, code)
        return False

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

    def _check_host(self) -> bool:
        """Reject requests without exactly one allowed local Host value."""
        hosts = self.headers.get_all("Host") or []
        port = self.server.server_port
        if len(hosts) != 1 or hosts[0] not in (
                f"127.0.0.1:{port}", f"localhost:{port}"):
            return self._reject("Forbidden: Host 不允许")
        return True

    def do_GET(self) -> None:
        if not self._check_host():
            return
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
        elif path == "/api/ip-intel/status":
            # Configuration-only response.  Never serialize the ProviderConfig
            # itself: it contains the in-memory credentials.
            self._json(_provider_status_payload())
        elif path in ("/api/leak/audits", "/api/leak/history"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                limit = int(qs.get("limit", ["20"])[0])
            except (TypeError, ValueError):
                limit = 20
            self._json(_load_leak_audits(limit))
        elif path == "/api/node":
            # 单节点详情：近 N 天测速序列 + 出口 IP 变化时间线（SQL 参数化防注入）。
            # key= 按 node_key 查（订阅改名不断链）；无 key 时按 name，兼容旧行为。
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = qs.get("name", [""])[0]
            key = qs.get("key", [""])[0]
            if not name and not key:
                self._json({"ok": False, "msg": "缺少 name 参数"}, 400)
                return
            days = _days_param(qs)
            sync_db()
            self._json({
                "series": speedbench_db.node_series(db_path(), name, days=days,
                                                    node_key=key),
                "ip_changes": (speedbench_db.ip_changes(db_path(), name)
                               if name else []),
                "ip_reputation_changes": _load_ip_reputation_changes(
                    name=name, node_key=key),
            })
        elif path == "/api/subscriptions":
            # 订阅维度汇总：按 provider 聚合近 N 天的可用率/速度/评分
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            days = _days_param(qs)
            sync_db()
            out = speedbench_db.subscription_summary(db_path(), days=days)
            for item in out:
                if not item["provider"]:
                    item["provider"] = UNKNOWN_PROVIDER
            self._json(out)
        elif path == "/api/subscription":
            # 单订阅逐轮趋势：name 为 "(未知订阅)" 或空串时查 provider=''
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = qs.get("name", [""])[0]
            provider = "" if name in ("", UNKNOWN_PROVIDER) else name
            days = _days_param(qs)
            sync_db()
            self._json(speedbench_db.subscription_series(db_path(), provider,
                                                         days=days))
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
            return self._reject("Forbidden: 令牌无效")
        if not self._check_host():
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return self._reject("Forbidden: Origin 不允许")
        return True

    def do_POST(self) -> None:
        if not self._check_post():
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/run":
            with STATE_LOCK:
                busy = STATE["running"]
            if busy:
                self._reject("已有测速任务进行中", 409)
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
        elif path == "/api/ip-intel/settings":
            ok, msg = _set_ip_intel_settings(self._read_body())
            self._json({"ok": ok, "msg": msg}, 200 if ok else 400)
        elif path == "/api/leak/evaluate":
            payload = self._read_body()
            self._json(_evaluate_leak_payload(payload))
        elif path in ("/api/leak/audit", "/api/leak/save"):
            body = self._read_body()
            result = _evaluate_leak_payload(body)
            # Invalid payloads are never persisted.  The evaluation itself is
            # still returned so the browser can explain the failure.
            if result.get("msg"):
                self._json(result, 400)
                return
            evaluation = speedbench_leak.LeakEvaluation(
                status=str(result.get("status", "unknown")),
                status_text=str(result.get("status_text", "无法确认")),
                complete=bool(result.get("complete", False)),
                candidates=list(result.get("candidates") or []),
                public_candidates=list(result.get("public_candidates") or []),
                warnings=list(result.get("warnings") or []),
                notes=list(result.get("notes") or []),
                compared=bool(result.get("compared", False)),
                exit_ipv4=result.get("exit_ipv4"),
                exit_ipv6=result.get("exit_ipv6"),
            )
            dns_status = body.get("dns_status") if isinstance(body, dict) else None
            if dns_status not in {"clear", "warning", "unknown"}:
                dns_status = None
            saved = _save_leak_audit(speedbench_leak.make_audit_record(
                evaluation, dns_status=dns_status))
            result["persistence"] = saved
            self._json(result)
        elif path == "/api/quit":
            with STATE_LOCK:
                busy = STATE["running"]
            if busy:
                cancel_benchmark()  # 先中断测速（SIGINT/哨兵文件走 finally 恢复 Clash 配置），再停面板
            msg = "面板已停止" + ("，已先中断进行中的测速" if busy else "")
            self._json({"ok": True, "msg": msg})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self._reject("not found", 404)


def main() -> int:
    # Windows 非中文区域设置下 stdout 默认 cp1252，print 中文启动信息会直接
    # UnicodeEncodeError 崩掉面板（CI windows-latest 实测）。只把 errors 钉成
    # replace：GBK 中文控制台行为不变（该编码能表示中文），cp1252 下退化
    # 成 ? 但不崩——真正的用户界面在浏览器里，控制台文案仅是辅助。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass  # 非 TextIOWrapper 环境（IDLE/嵌入式）没有 reconfigure，跳过即可

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
    write_token_file()
    print("Ctrl+C 停止。测速期间 Mihomo 会临时切到 GLOBAL 模式，结束自动恢复。")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    def _tray_quit() -> None:
        # 托盘「退出」与 /api/quit 同一语义：先中断进行中的测速
        # （走 finally 恢复 Clash 配置），再停面板
        with STATE_LOCK:
            busy = STATE["running"]
        if busy:
            cancel_benchmark()
        threading.Thread(target=server.shutdown, daemon=True).start()

    # Windows：进程内系统托盘（零依赖 ctypes/Win32，见 speedbench_tray.py）；
    # 非 win32 下 start_tray 是 no-op 返回 None
    tray = speedbench_tray.start_tray(HERE / "speedbench.ico",
                                      on_open=lambda: webbrowser.open(url),
                                      on_quit=_tray_quit)
    if tray is not None:
        print("托盘图标已启动：左键打开面板，右键可退出 SpeedBench。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        speedbench_tray.stop_tray(tray)  # 摘托盘图标，避免僵尸图标
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
