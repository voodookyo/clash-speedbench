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
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as _ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from clash_speedbench import (  # noqa: E402
    CLEANUP_FAILED_EXIT,
    build_selectable_graph,
    connect_controller as core_connect_controller,
    pick_switch_group,
)
import speedbench_db  # noqa: E402
import speedbench_ip_intel  # noqa: E402
import speedbench_leak  # noqa: E402
import speedbench_tray  # noqa: E402
import speedbench_controller  # noqa: E402
import speedbench_sources  # noqa: E402
import speedbench_tasks  # noqa: E402
from speedbench_jobs import JobStore, JobError, TERMINAL, _result as safe_job_result
from speedbench_progress import PREFIX, parse_record
import speedbench_power as power
from speedbench_owner import BackendLease, LeaseError
from speedbench_preferences import Preferences, PreferenceError
from speedbench_transfer import HistoryTransfer, TransferError, PENDING as IMPORT_PENDING
import speedbench_releases
from speedbench_config import ENV as ROOT_ENV, RootChoice, validate_root, ConfigRootError


class ThreadingHTTPServer(_ThreadingHTTPServer):
    """Numeric loopback binding needs no reverse DNS to start the panel."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]


SCRIPT = HERE / "clash_speedbench.py"
# 数据目录：默认脚本同级；打包成 .app 时由启动器用 SPEEDBENCH_HOME 指到
# ~/Library/Application Support/ClashSpeedBench，避免污染应用包。
DATA_HOME = Path(os.environ.get("SPEEDBENCH_HOME", str(HERE)))
HISTORY = DATA_HOME / "speedbench-history.jsonl"
# 「停止测速」哨兵文件：面板无控制台（pythonw），CTRL_BREAK_EVENT 无处可投，
# 改写哨兵文件，测速子进程在节点/轮次间隙发现后走 KeyboardInterrupt 优雅中断。
CANCEL_FILE = DATA_HOME / "cancel-request"

# 前端静态文件目录与分发白名单：URL 路径 → (磁盘文件名, MIME)。
# 只认列出的静态文件、不做任何路径拼接，其余一律 404。
WEB_DIR = HERE / "web"
STATIC_FILES = {
    '/static/config-root.js': ('config-root.js','application/javascript; charset=utf-8'),
    '/static/releases.js': ('releases.js','application/javascript; charset=utf-8'),
    '/static/preferences.js': ('preferences.js','application/javascript; charset=utf-8'),
    '/static/profiles.js': ('profiles.js','application/javascript; charset=utf-8'),
    '/static/view.js': ('view.js','application/javascript; charset=utf-8'),
    '/static/history-view.js': ('history-view.js','application/javascript; charset=utf-8'),
    '/static/tasks.js': ('tasks.js','application/javascript; charset=utf-8'),
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
    "cleanup_incomplete": False,
    "lines": [],
    "started": None,
    "exit_code": None,
    "proc": None,
}
STATE_LOCK = threading.Lock()
JOBS = JobStore()

MAX_LINES = 500

# 每次启动随机生成的写操作令牌：注入页面 <meta>，所有 POST 必须携带，
# 防止其他网页跨站向本地面板发写请求（CSRF）。
WEB_TOKEN = secrets.token_hex(16)
DESKTOP_IDENTITY = None
DESKTOP_ACTIONS = None
DESKTOP_SHUTDOWN = None
DESKTOP_EXITING = None
# Private desktop backends own exactly one PowerMonitor (native ResumeGuard +
# bounded polling thread). The ordinary browser backend leaves this None and
# starts no thread or clock detection at all.
DESKTOP_POWER = None
POWER_REASONS = ('manual', 'system_resume', 'power_clock_error')
_POWER_COUNTER_REASON = {'system_resume': 'system_resumes',
                         'power_clock_error': 'power_clock_errors'}
POWER_CLOCK_FAILED_MSG = ('本机挂起/恢复时钟不可用；为避免系统休眠干扰测量，'
                          '已停止接受新任务。请重启应用后重试。')
RELEASE_CHECKER = speedbench_releases.ReleaseChecker()
CONFIG_ROOT = RootChoice(os.environ.get(ROOT_ENV,''))
DATA_OWNER = None
_TRANSFER = None


class PowerMonitor:
    """Private desktop-only suspend/resume watcher.

    Exactly one native ResumeGuard and one bounded polling thread are owned
    here. The thread never signals a process or mutates state directly: it
    reserves an exact current job through the single shared STATE_LOCK helper
    (which consumes the one-shot guard poll) and only then calls the existing
    exact-job cooperative cancellation path. There is deliberately no retry,
    restart or alternate scheduling architecture.
    """

    DEFAULT_INTERVAL = 0.25

    def __init__(self, clock, interval=None):
        self.guard = power.ResumeGuard(clock)
        interval = self.DEFAULT_INTERVAL if interval is None else interval
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0:
            interval = self.DEFAULT_INTERVAL
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name='speedbench-power', daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def join(self, timeout=None):
        if self._thread.is_alive():
            self._thread.join(timeout)

    def arm(self, job_id):
        self.guard.arm(job_id)

    def disarm(self, job_id):
        self.guard.disarm(job_id)

    def poll(self):
        return self.guard.poll()

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                with STATE_LOCK:
                    reserved = _reserve_power_cancellation_locked()
                if reserved is not None:
                    job_id, reason = reserved
                    cancel_benchmark(expected_job_id=job_id, reason=reason)
            except Exception:
                # An unexpected watcher failure must fail closed: never keep
                # measuring unmonitored.
                with STATE_LOCK:
                    STATE['power_clock_failed'] = True
                    current = STATE.get('job_id') if STATE.get('running') else None
                if current:
                    cancel_benchmark(expected_job_id=current, reason='power_clock_error')
                return


def history_transfer():
    global _TRANSFER
    home = DATA_HOME.resolve()
    if _TRANSFER is None or _TRANSFER.home != home:
        _TRANSFER = HistoryTransfer(home)
    return _TRANSFER


def recover_history_import():
    """Run under the existing backend lease, before any startup data writes."""
    with _DB_SYNC_LOCK:
        return history_transfer().recover(DATA_OWNER)


def connect_controller(*args,**kwargs):
    root,_revision=CONFIG_ROOT.snapshot()
    if root or ROOT_ENV in os.environ:
        kwargs.setdefault('config_root',root)
    return core_connect_controller(*args,**kwargs)


def _release_version():
    return (DESKTOP_IDENTITY or {}).get('version', speedbench_releases.CORE_VERSION)

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
    text = speedbench_controller.redact_text(value)
    root,_revision=CONFIG_ROOT.snapshot()
    if isinstance(root,str) and root:
        for private in (root,root.replace('\\','/'),json.dumps(root,ensure_ascii=False)[1:-1]):
            text=text.replace(private,'[自定义配置目录]')
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
_DB_SYNC_LOCK = threading.RLock()
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
    records=speedbench_db.all_runs(db_path())
    summaries=speedbench_db.history_task_summaries(db_path(),
        [rec['task'].get('job_id') for rec in records if isinstance(rec.get('task'),dict)])
    for rec in records:
        task={}
        raw=rec.get('task')
        if isinstance(raw,dict):
            if raw.get('mode') in speedbench_tasks.MODES:task['mode']=raw['mode']
            if raw.get('target_profile') in speedbench_tasks.PROFILES:task['target_profile']=raw['target_profile']
            if isinstance(raw.get('partial'),bool):task['partial']=raw['partial']
            if raw.get('status') in ('completed','cancelled','failed','interrupted'):task['status']=raw['status']
            count=raw.get('selected_node_count')
            if isinstance(count,int) and not isinstance(count,bool) and 0<=count<=3000:task['selected_node_count']=count
            summary=summaries.get(raw.get('job_id')) if isinstance(raw.get('job_id'),str) else None
            if summary:
                # Late cleanup failure/cancellation cannot be hidden by a raw
                # report saved just before the backend reached its terminal.
                partial=task.get('partial',False) or summary['partial']
                task.update(summary);task['partial']=partial
        out.append({
            "ts": rec.get("ts", ""),
            **({'task':task} if isinstance(raw,dict) else {}),
            "results": [
                {
                    "name": r.get("name"),
                    "provider": r.get("provider") or "",
                    "median_mbps": r.get("median_mbps"),
                    "latency_ms": r.get("latency_ms"),
                    "score": r.get("score"),
                    **speedbench_sources.result_origin(r),
                    **{k:v for k,v in safe_job_result(r).items() if k in (
                        'network_score','probe_attempts','probe_successes','probe_failures',
                        'probe_loss_pct','measurement_scope','exit_status','ip_grade','ip_quality_score',
                        'metric_updated_at','measured_metric_count')+
                        (('jitter_ms','multi_mbps','intel_v4','intel_v6','ip') if
                         isinstance(rec.get('task'),dict) or r.get('measurement_scope') else ())},
                }
                for r in rec.get("results", [])
            ],
        })
    return out


def benchmark_command(params):
    cmd = [sys.executable, '-u', str(SCRIPT), '--history', str(HISTORY), '--non-interactive']
    confirmed=params.get('allow_serial') is True
    if not confirmed:cmd.append('--deny-serial-fallback')
    if (params.get('workers') or 6)>1 or confirmed:cmd.append('--yes')
    if sys.flags.dont_write_bytecode:cmd.insert(1,'-B')
    if params.get('mode') and params['mode'] != 'legacy':
        cmd += ['--mode',params['mode']]
    if params.get('target_profile'):
        cmd += ['--target-profile',params['target_profile']]
    if params.get('include'):
        cmd += ['--include',params['include']]
    for key in speedbench_tasks.LIMITS:
        if params.get(key) is not None:
            cmd += ['--'+key.replace('_','-'),str(params[key])]
    for key in speedbench_tasks.BOOLEAN_OPTIONS + ('auto_switch',):
        if params.get(key):
            cmd += ['--'+key.replace('_','-')]
    for source_id in params.get('subscription_ids',[]):
        cmd += ['--subscription-id',source_id]
    for node_id in params.get('node_ids',[]):
        cmd += ['--node-id',node_id]
    return cmd


def validate_run_params(params):
    if not isinstance(params,dict):
        raise speedbench_tasks.TaskConfigError('请求必须是对象')
    control = {'include','auto_switch','subscription_ids','node_ids','allow_serial'}
    config = {k:v for k,v in params.items() if k not in control}
    resolved = speedbench_tasks.resolve_config(config)
    if 'allow_serial' in params and not isinstance(params['allow_serial'],bool):
        raise speedbench_tasks.TaskConfigError('串行确认必须是布尔值')
    if resolved.workers<=1 and params.get('allow_serial') is not True:
        raise speedbench_tasks.TaskConfigError('串行模式会切换 GLOBAL，请先在界面确认后再启动')
    if params.get('allow_serial') is True and (resolved.mode!='legacy' or resolved.workers!=1):
        raise speedbench_tasks.TaskConfigError('串行确认仅用于兼容串行模式；隔离 worker 任务不会自动回退')
    if resolved.mode != 'legacy' and resolved.workers <= 1:
        raise speedbench_tasks.TaskConfigError('新模式需要隔离 worker')
    if ('auto_switch' in params and not isinstance(params['auto_switch'],bool)):
        raise speedbench_tasks.TaskConfigError('自动切换参数无效')
    pattern = params.get('include','')
    if not isinstance(pattern,str) or len(pattern)>1000:
        raise speedbench_tasks.TaskConfigError('筛选表达式无效')
    try:
        re.compile(pattern)
    except re.error:
        raise speedbench_tasks.TaskConfigError('筛选表达式无效') from None
    return params


def _reserve_cancellation_locked(job_id, reason):
    """Reserve once while STATE_LOCK protects the current task and terminal decision."""
    if STATE.get('cancel_requested'):
        return False
    STATE['cancel_requested'] = True
    STATE['cancel_reason'] = reason
    if job_id:
        if reason in _POWER_COUNTER_REASON:
            JOBS.publish(job_id,'phase_finished',phase='cleanup',
                payload={'metrics':{'cleanup':{'counters':{_POWER_COUNTER_REASON[reason]:1}}}})
        JOBS.transition(job_id, 'cancelling')
    return True


def _reserve_power_cancellation_locked():
    """Caller MUST hold STATE_LOCK.

    Single shared reservation helper for the desktop watcher and the runner's
    terminal decision. It consumes the one-shot native guard poll and, for the
    exact current active job, atomically reserves cancellation, the fixed
    reason and the one-time interruption counter before the lock is released.
    Returns (job_id, reason) when newly reserved, else None. Native clock
    failure permanently marks this backend and reserves the error reason for
    the current job too.
    """
    monitor = DESKTOP_POWER
    if monitor is None or STATE.get('power_clock_failed'):
        return None
    current = STATE.get('job_id')
    active = bool(STATE.get('running') and current)
    failed = False
    detected = None
    try:
        detected = monitor.poll()
    except power.PowerClockError:
        STATE['power_clock_failed'] = True
        failed = True
    if not active:
        return None
    if failed:
        detected = current
    if detected != current:
        return None
    # Reserve at most once per job: duplicate detections or a manual cancel
    # must never increment interruption counters repeatedly.
    if STATE.get('cancel_requested'):
        return None
    try:
        if JOBS.snapshot(current)['status'] in TERMINAL:
            return None
    except JobError:
        return None
    reason = 'power_clock_error' if failed else 'system_resume'
    _reserve_cancellation_locked(current, reason)
    return current, reason


def run_benchmark(params: dict) -> None:
    # -u：子进程 stdout 走管道时默认块缓冲，进度行会堵在缓冲区里，
    # 面板看不到实时进度；无缓冲模式让每行立即到达。
    cmd = benchmark_command(params)
    job_id = params.get('_job_id')
    source_seq = 0
    checkpoint_at = 0
    proc = None
    delegation = None
    unreaped = False

    def close_private_pipe():
        if delegation is not None and proc is not None and proc.stdin is not None:
            try:proc.stdin.close()
            except (OSError,ValueError):pass

    def checkpoint():
        try:
            speedbench_db.save_task(db_path(),JOBS.snapshot(job_id))
        except Exception as error:
            with STATE_LOCK:
                STATE['lines'].append('!! 任务检查点保存失败: '+_redact_runtime_text(error))

    with STATE_LOCK:
        STATE["running"] = True
        STATE["lines"] = ["$ " + " ".join(os.path.basename(c) if c == str(SCRIPT) else c for c in cmd)]
        STATE["started"] = time.time()
        STATE["exit_code"] = None

    try:
        if job_id:
            with STATE_LOCK:
                requested = STATE.get('cancel_requested',False)
            if requested:
                return
            JOBS.transition(job_id,'preparing')
            checkpoint()
        # No hidden getpass prompt or benchmark spawn when authentication fails.
        # The child resolves fresh local config itself; no auto key in argv/env.
        root=params.get('_config_root')
        if root is None:
            connect_controller()
        else:
            connect_controller(config_root=root)
        if job_id:
            with STATE_LOCK:
                requested = STATE.get('cancel_requested',False)
            if requested:
                return
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
        if root is not None:
            env.pop(ROOT_ENV,None)
            if root:env[ROOT_ENV]=root
        env["SPEEDBENCH_CANCEL_FILE"] = str(CANCEL_FILE)
        if job_id:
            env['SPEEDBENCH_JOB_ID'] = job_id
            env['SPEEDBENCH_CANCEL_PRIMED'] = '1'
        delegation=DATA_OWNER.delegation(HISTORY) if DATA_OWNER is not None else None
        if delegation is not None:
            cmd.append('--backend-child')
            popen_kwargs['stdin']=subprocess.PIPE
            # Resolve the root before changing subprocess cwd. Relative caller
            # environment values must not point the child at a different home.
            env['SPEEDBENCH_HOME']=str(DATA_OWNER.path.parent)
        proc = subprocess.Popen(
            cmd, cwd=str(DATA_HOME), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            **popen_kwargs,
        )
        with STATE_LOCK:
            STATE["proc"] = proc
            requested = bool(job_id and STATE.get('cancel_requested',False))
        if delegation is not None:
            # Binary frame bypasses platform console encodings. Keep the pipe
            # open for this task's lifetime; EOF lets the child detect a crash.
            proc.stdin.buffer.write(delegation);proc.stdin.buffer.flush()
        if requested:
            CANCEL_FILE.write_text('cancel',encoding='utf-8')
        assert proc.stdout is not None
        for line in proc.stdout:
            line = _redact_runtime_text(line.rstrip("\n"))
            if job_id and line.startswith(PREFIX):
                record = parse_record(line,job_id)
                if record and record['source_seq'] > source_seq:
                    source_seq = record['source_seq']
                    try:
                        if record['type'] == 'phase_started':
                            if JOBS.snapshot(job_id)['status'] != 'cancelling':
                                JOBS.transition(job_id,record['phase'])
                        elif record['type'].startswith('node_') or record['type'] in ('phase_finished','milestone'):
                            JOBS.publish(job_id,record['type'],phase=record['phase'],
                                         node_id=record['node_id'],payload=record['payload'])
                    except JobError:
                        # Invalid/out-of-order records cannot change the owner
                        # or expose raw payloads as human-readable logs.
                        pass
                    if record['type']=='phase_started' or time.monotonic()-checkpoint_at>=1:
                        checkpoint()
                        checkpoint_at = time.monotonic()
                continue
            with STATE_LOCK:
                STATE["lines"].append(line)
                if len(STATE["lines"]) > MAX_LINES:
                    STATE["lines"] = STATE["lines"][-MAX_LINES:]
        STATE["exit_code"] = proc.wait()
    except Exception as e:
        if delegation is not None and proc is not None:
            close_private_pipe() # EOF requests graceful cancellation/cleanup.
            try:proc.wait(timeout=8)
            except (subprocess.TimeoutExpired,OSError):
                unreaped=True
                with STATE_LOCK:
                    STATE['cleanup_incomplete']=True
                    STATE['lines'].append('!! 子任务尚未退出；保留进程所有权并禁止启动新任务，不能确认清理成功。')
        with STATE_LOCK:
            STATE["lines"].append(f"!! 启动测速失败: {_redact_runtime_text(e)}")
            STATE["exit_code"] = -1
    finally:
        close_private_pipe()
        # 测速进程已把本轮结果追加进 jsonl，顺手增量入库；失败不影响面板状态
        try:
            sync_db()
        except Exception as e:
            with STATE_LOCK:
                STATE["lines"].append(f"!! 历史入库失败: {_redact_runtime_text(e)}")
        if job_id:
            with STATE_LOCK:
                # Consume the one-shot guard poll and reserve cancellation for
                # this exact job immediately before the terminal decision, even
                # though the child may already have exited normally. A resume
                # that landed before the next periodic poll therefore still
                # becomes a partial terminal result.
                _reserve_power_cancellation_locked()
                cancelled = bool(STATE.get('cancel_requested',False))
                exit_code = STATE['exit_code']
                status = JOBS.snapshot(job_id)['status']
                if proc is not None and not unreaped and exit_code in (0,130):
                    JOBS.complete_cleanup(job_id)
                if exit_code == CLEANUP_FAILED_EXIT:
                    STATE['cleanup_incomplete']=True
                    STATE['lines'].append('!! 临时 worker 清理未完成；保留部分结果，任务标记失败，不能确认取消成功。')
                    JOBS.transition(job_id,'failed')
                elif cancelled or exit_code == 130:
                    JOBS.transition(job_id,'cancelling')
                    JOBS.transition(job_id,'cancelled')
                elif exit_code == 0 and status == 'finalizing':
                    JOBS.transition(job_id,'completed')
                else:
                    JOBS.transition(job_id,'failed')
            checkpoint()
            if DESKTOP_POWER is not None:
                DESKTOP_POWER.disarm(job_id)
        with STATE_LOCK:
            STATE["running"] = unreaped
            STATE["proc"] = proc if unreaped else None
        if unreaped:
            # Keep the original handle, never look up a possibly reused PID.
            # This watcher cannot clear a newer task's state or certify worker
            # cleanup; the fail-closed flag remains until backend restart.
            def wait_owned_child():
                try:proc.wait()
                except OSError:return
                with STATE_LOCK:
                    if STATE.get('proc') is proc:
                        STATE['proc']=None;STATE['running']=False
            threading.Thread(target=wait_owned_child,daemon=True).start()


def cancel_benchmark(*, expected_job_id=None, reason='manual') -> dict:
    """中断正在运行的测速子进程。

    POSIX 发 SIGINT；Windows 写哨兵文件（面板无控制台后 CTRL_BREAK_EVENT
    无处可投；测速子进程在节点/轮次间隙检查 cancel_requested，发现后转
    KeyboardInterrupt）——两者都走 clash_speedbench.py 的 finally 恢复
    Clash 策略组/模式。Windows 的 terminate 是 TerminateProcess，不跑
    finally，所以只作兜底：最多等 5 秒，未退出再 terminate（再兜底 kill）。

    ``expected_job_id`` (internal, not a public IPC parameter) pins the exact
    job under STATE_LOCK; ``reason`` is one of POWER_REASONS. The cooperative
    signal/sentinel is only delivered while that ownership check is still
    locked, so a wake for an old job can never cancel a replacement job. The
    bounded wait/terminate fallback runs on the captured owned process outside
    STATE_LOCK. No process-name lookup is ever used.
    """
    if reason not in POWER_REASONS:
        return {'ok': False, 'msg': '中断原因无效'}
    with STATE_LOCK:
        proc = STATE.get("proc")
        running = STATE["running"]
        job_id = STATE.get('job_id')
        if expected_job_id is not None and job_id != expected_job_id:
            return {'ok': False, 'msg': '任务已失效，请刷新'}
        if not running:
            return {"ok": False, "msg": "当前没有正在进行的测速"}
        if job_id:
            try:
                if JOBS.snapshot(job_id)['status'] in TERMINAL:
                    return {'ok': False, 'msg': '任务已结束'}
            except JobError:
                return {'ok': False, 'msg': '任务已失效，请刷新'}
            try:
                _reserve_cancellation_locked(job_id, reason)
            except JobError:
                return {'ok': False, 'msg': '任务已失效，请刷新'}
            if proc is None:
                return {'ok': True, 'msg': '已取消等待启动的任务；完成清理后会结束'}
        if proc is None or proc.poll() is not None:
            return {"ok": False, "msg": "当前没有正在进行的测速"}
        # Ownership is verified and still locked: only now write the shared
        # sentinel / signal the captured owned process.
        try:
            if sys.platform == "win32":
                CANCEL_FILE.write_text(str(int(time.time())), encoding="utf-8")
            else:
                proc.send_signal(signal.SIGINT)
        except OSError as e:
            return {"ok": False, "msg": f"中断失败: {e}"}
    # Wait/terminate the captured process outside STATE_LOCK.
    try:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        with STATE_LOCK:
            if STATE.get('job_id') == job_id:
                message = {'manual':'!! 测速已被手动中断',
                    'system_resume':'!! 系统休眠／唤醒中断本轮测量；保留部分结果，不会自动重试。',
                    'power_clock_error':'!! 无法监测系统休眠／唤醒；保留部分结果，请重启应用。'}
                STATE["lines"].append(message.get(STATE.get('cancel_reason'), message[reason]))
        return {"ok": True, "msg": "已请求中断测速；请以任务终态和恢复记录确认清理结果"}
    except Exception as e:
        return {"ok": False, "msg": f"中断失败: {e}"}


def get_catalog(api=None, snapshot=None, config_root=None):
    try:
        root=CONFIG_ROOT.snapshot()[0] if config_root is None else config_root
        api = api if api is not None else connect_controller(config_root=root)
        options={'snapshot':snapshot,'config_root':root}
        if root:options['config_file']=str(Path(root)/'clash-verge.yaml')
        return speedbench_sources.discover_catalog(api, HISTORY.parent, **options)
    except Exception:
        return dict(version=2, status='controller_unavailable', sources=[], nodes=[])


def _check_source_selection(params):
    if not isinstance(params, dict):
        return False
    for key, prefix in (('subscription_ids','subscription'), ('node_ids','node')):
        values = params.get(key, [])
        if (not isinstance(values, list) or len(values) > 1000 or
                any(not isinstance(x,str) or not re.fullmatch(prefix+r'_v2_[0-9a-f]{32}', x) for x in values)):
            return False
    if params.get('subscription_ids') or params.get('node_ids'):
        catalog = get_catalog()
        sources = {s['subscription_id'] for s in catalog['sources'] if s['loaded']}
        nodes = {n['node_id'] for n in catalog['nodes']}
        if not set(params.get('subscription_ids', [])).issubset(sources):
            return False
        if not set(params.get('node_ids', [])).issubset(nodes):
            return False
    return True


# Fresh-confirmation plan contract.  Only these exact keys may cross the wire,
# and only these types are accepted back.  Paths, credentials, connection
# definitions and raw configuration never appear here.
_SWITCH_PLAN_STRINGS = ('node_id', 'runtime_name', 'identity_strength', 'source_status',
                        'subscription_name', 'group', 'current')
_SWITCH_PLAN_FIELDS = frozenset(_SWITCH_PLAN_STRINGS + ('subscription_ids', 'subscriptions',
                                                        'root_revision'))
_SWITCH_STALE_MSG = '切换信息已变化，请刷新目录后重新确认切换'
_SUB_ID_RE = re.compile(r'subscription_v2_[0-9a-f]{32}\Z')
_NODE_ID_RE = re.compile(r'node_v2_[0-9a-f]{32}\Z')


def _resolve_switch_node(catalog, *, name='', node_id=''):
    """Resolve exactly one current catalogue node; never guess on ambiguity."""
    nodes = catalog.get('nodes') if isinstance(catalog, dict) else None
    nodes = nodes if isinstance(nodes, list) else []
    if node_id:
        matches = [n for n in nodes if isinstance(n, dict) and n.get('node_id') == node_id]
        if len(matches) != 1:
            return None, '节点身份已失效，请刷新目录后重试'
        return matches[0], ''
    if name:
        matches = [n for n in nodes if isinstance(n, dict) and n.get('runtime_name') == name]
        if not matches:
            return None, '目录中找不到该节点，请刷新后重试'
        if len(matches) > 1:
            return None, '该名称对应多个节点，无法唯一确认，请刷新目录后重试'
        return matches[0], ''
    return None, '缺少节点名或节点身份'


def _switch_plan(node, group, current, revision):
    subscriptions = []
    for item in node.get('subscriptions') or []:
        if isinstance(item, dict) and isinstance(item.get('subscription_id'), str):
            subscriptions.append({'subscription_id': item['subscription_id'],
                                  'name': item.get('name') if isinstance(item.get('name'), str) else ''})
    return {
        'node_id': str(node.get('node_id') or ''),
        'runtime_name': str(node.get('runtime_name') or ''),
        'identity_strength': str(node.get('identity_strength') or ''),
        'source_status': str(node.get('source_status') or ''),
        'subscription_name': str(node.get('subscription_name') or ''),
        'subscription_ids': [x for x in (node.get('subscription_ids') or []) if isinstance(x, str)],
        'subscriptions': subscriptions,
        'group': str(group or ''),
        'current': '' if current is None else str(current),
        'root_revision': int(revision),
    }


def _valid_switch_plan(plan) -> bool:
    if not isinstance(plan, dict) or frozenset(plan) != _SWITCH_PLAN_FIELDS:
        return False
    if any(not isinstance(plan.get(k), str) for k in _SWITCH_PLAN_STRINGS):
        return False
    if not _NODE_ID_RE.fullmatch(plan['node_id']):
        return False
    if plan['identity_strength'] not in ('strong', 'weak'):
        return False
    if plan['source_status'] not in ('verified', 'ambiguous', 'unknown'):
        return False
    if any(len(plan[k]) > 4096 for k in ('runtime_name', 'group', 'current', 'subscription_name')):
        return False
    if not plan['group']:
        return False
    ids = plan['subscription_ids']
    if (not isinstance(ids, list) or len(ids) > 100 or
            any(not isinstance(x, str) or not _SUB_ID_RE.fullmatch(x) for x in ids)):
        return False
    subs = plan['subscriptions']
    if not isinstance(subs, list) or len(subs) > 100:
        return False
    for item in subs:
        if (not isinstance(item, dict) or frozenset(item) != {'subscription_id', 'name'} or
                not isinstance(item['name'], str) or len(item['name']) > 4096 or
                not isinstance(item['subscription_id'], str) or
                not _SUB_ID_RE.fullmatch(item['subscription_id'])):
            return False
    return type(plan['root_revision']) is int and 0<=plan['root_revision']<2**63


def _resolve_group(proxies, runtime_name):
    graph = build_selectable_graph(proxies)
    group = pick_switch_group(proxies, graph, runtime_name, "GLOBAL")
    if not group:
        return None, f"找不到包含 {runtime_name} 的 Selector 组"
    return group, ''


def preview_switch(name: str = '', node_id: str = '') -> dict:
    """Read-only fresh plan.  Never selects or writes to the controller."""
    try:
        root, revision = CONFIG_ROOT.snapshot()
        api = connect_controller(config_root=root)
        proxies = api.get("/proxies").get("proxies", {})
        if not isinstance(proxies, dict):
            return {'ok': False, 'msg': '无法确认当前节点，请刷新后重试'}
        catalog = get_catalog(api, snapshot=proxies, config_root=root)
        node, error = _resolve_switch_node(catalog, name=name, node_id=node_id)
        if error:
            return {'ok': False, 'msg': error}
        runtime_name = str(node.get('runtime_name') or '')
        if runtime_name not in proxies:
            return {'ok': False, 'msg': '节点身份已失效，请刷新目录后重试'}
        group, error = _resolve_group(proxies, runtime_name)
        if error:
            return {'ok': False, 'msg': error}
        current = proxies.get(group, {}).get("now")
        plan=_switch_plan(node, group, current, revision)
        if not _valid_switch_plan(plan):
            return {'ok':False,'msg':'节点目录暂不可核验，请刷新后重试'}
        with STATE_LOCK:
            if CONFIG_ROOT.snapshot()[1]!=revision:
                return {'ok':False,'msg':_SWITCH_STALE_MSG}
            return {'ok': True, 'plan': plan}
    except Exception as e:
        return {'ok': False, 'msg': _redact_runtime_text(e)}


def _confirmed_switch(plan: dict) -> dict:
    """Re-resolve fresh state and require an exact match before any select."""
    try:
        root, revision = CONFIG_ROOT.snapshot()
        api = connect_controller(config_root=root)
        proxies = api.get("/proxies").get("proxies", {})
        if not isinstance(proxies, dict):
            return {'ok': False, 'msg': _SWITCH_STALE_MSG}
        catalog = get_catalog(api, snapshot=proxies, config_root=root)
        node, error = _resolve_switch_node(catalog, node_id=plan['node_id'],
                                           name=plan['runtime_name'])
        if error:
            return {'ok': False, 'msg': _SWITCH_STALE_MSG}
        runtime_name = str(node.get('runtime_name') or '')
        if runtime_name not in proxies:
            return {'ok': False, 'msg': _SWITCH_STALE_MSG}
        group, error = _resolve_group(proxies, runtime_name)
        if error:
            return {'ok': False, 'msg': _SWITCH_STALE_MSG}
        current = proxies.get(group, {}).get("now")
        if _switch_plan(node, group, current, revision) != plan:
            return {'ok': False, 'msg': _SWITCH_STALE_MSG}
        with STATE_LOCK:
            if CONFIG_ROOT.snapshot()[1] != revision:
                return {'ok': False, 'msg': _SWITCH_STALE_MSG}
            if current == runtime_name:
                return {"ok": True, "msg": f"{group} 已是 {runtime_name}", "group": group, "now": runtime_name}
            api.select(group, runtime_name)
        return {"ok": True, "msg": f"已切换 {group} → {runtime_name}", "group": group, "now": runtime_name}
    except Exception as e:
        return {"ok": False, "msg": _redact_runtime_text(e)}


def do_switch(name: str, node_id: str = '', confirmation=None) -> dict:
    if confirmation is not None:
        if not _valid_switch_plan(confirmation) or node_id!=confirmation['node_id']:
            return {'ok':False,'msg':_SWITCH_STALE_MSG}
        return _confirmed_switch(confirmation)
    try:
        root,revision=CONFIG_ROOT.snapshot()
        api = connect_controller(config_root=root)
        proxies = api.get("/proxies").get("proxies", {})
        if node_id:
            catalog = get_catalog(api, snapshot=proxies,config_root=root)
            matches = [n for n in catalog['nodes'] if n['node_id'] == node_id]
            if len(matches) != 1 or matches[0]['runtime_name'] not in proxies:
                return {'ok': False, 'msg': '节点身份已失效，请刷新目录后重试'}
            name = matches[0]['runtime_name']
        graph = build_selectable_graph(proxies)
        group = pick_switch_group(proxies, graph, name, "GLOBAL")
        if not group:
            return {"ok": False, "msg": f"找不到包含 {name} 的 Selector 组"}
        current = proxies.get(group, {}).get("now")
        with STATE_LOCK:
            if CONFIG_ROOT.snapshot()[1]!=revision:
                return {'ok':False,'msg':'配置目录已改变，请刷新后重新确认切换'}
            if current == name:
                return {"ok": True, "msg": f"{group} 已是 {name}", "group": group, "now": name}
            api.select(group, name)
        return {"ok": True, "msg": f"已切换 {group} → {name}", "group": group, "now": name}
    except Exception as e:
        return {"ok": False, "msg": _redact_runtime_text(e)}


def get_current() -> dict:
    """The main selector group (largest non-GLOBAL Selector) and its current node."""
    try:
        api = connect_controller()
        proxies = api.get("/proxies").get("proxies", {})
        graph = build_selectable_graph(proxies)
        cands = [g for g in graph
                 if g != "GLOBAL" and proxies.get(g, {}).get("type") == "Selector"]
        group = max(cands, key=lambda g: len(graph[g])) if cands else "GLOBAL"
        return {"ok": True, "group": group,
                "now": str(proxies.get(group, {}).get("now", ""))}
    except Exception as e:
        return {"ok": False, "msg": _redact_runtime_text(e)}


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
        self.send_header('Content-Security-Policy',
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self' https://api.ipify.org https://api6.ipify.org; "
            "img-src 'self' data:; object-src 'none'; frame-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(speedbench_controller.redact_payload(obj), ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _job_get(self,path,qs):
        try:
            if path == '/api/jobs':
                self._json(dict(version=1,active_job_id=JOBS.active_id(),latest=JOBS.latest()))
                return
            parts = path.split('/')
            if len(parts) not in (4,5) or not re.fullmatch(r'job_[0-9a-f]{32}',parts[3]):
                self._json({'ok':False,'msg':'任务不存在'},404)
                return
            job_id = parts[3]
            if len(parts)==4:
                self._json(JOBS.snapshot(job_id))
                return
            cursor = self.headers.get('Last-Event-ID') or qs.get('since_seq',['0'])[0]
            if not re.fullmatch(r'[0-9]{1,20}',cursor):
                raise JobError('事件游标无效')
            since = int(cursor)
            if parts[4]=='events':
                self._json(JOBS.read(job_id,since))
            elif parts[4]=='stream':
                # Resolve errors before sending streaming response headers.
                JOBS.read(job_id,since)
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream; charset=utf-8')
                self.send_header('Cache-Control','no-store')
                self.send_header('Connection','close')
                self.end_headers()
                deadline = time.monotonic()+20
                try:
                    while time.monotonic()<deadline:
                        delta = JOBS.wait(job_id,since,timeout=2)
                        if delta['resync']:
                            frames = [('snapshot',delta['seq'],delta['snapshot'])]
                        else:
                            frames = [('progress',e['seq'],e) for e in delta['events']]
                        for kind,seq,value in frames:
                            body = json.dumps(speedbench_controller.redact_payload(value),ensure_ascii=False,allow_nan=False)
                            self.wfile.write(f'id: {seq}\nevent: {kind}\ndata: {body}\n\n'.encode('utf-8'))
                        if not frames:
                            self.wfile.write(b': heartbeat\n\n')
                        self.wfile.flush()
                        since = delta['seq']
                        if JOBS.snapshot(job_id)['status'] in TERMINAL:
                            break
                except (BrokenPipeError,ConnectionResetError,OSError,JobError):
                    pass
                self.close_connection = True
            else:
                self._json({'ok':False,'msg':'任务接口不存在'},404)
        except JobError as e:
            self._json({'ok':False,'msg':str(e)},404 if 'unavailable' in str(e) else 400)

    def _read_body(self, *, strict=False):
        invalid=None if strict else {}
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return invalid
        # Avoid allowing a malformed client to make the handler read an
        # unbounded body.  The endpoint payloads are all tiny JSON objects.
        if length < 0 or length > 1024 * 1024:
            return invalid
        if not length:
            return invalid
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return invalid

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
        data_paths = {'/api/latest','/api/history','/api/catalog','/api/node',
            '/api/source','/api/sources/history','/api/subscriptions','/api/subscription',
            '/api/preferences','/api/leak/audits','/api/leak/history','/api/tasks',
            '/api/ip-intel/status'}
        if path in data_paths or path.startswith('/api/tasks/'):
            if self._data_busy():return
            with _DB_SYNC_LOCK:
                if self._data_busy():return
                self._get(path)
        else:
            self._get(path)

    def _data_busy(self):
        with STATE_LOCK:
            busy = STATE.get('importing') or STATE.get('import_failed')
        if busy:
            self._reject('历史导入或恢复尚未结束，请保留私有备份并等待恢复',409)
        return bool(busy)

    def _get(self, path):
        if path in ("/", "/index.html"):
            self._serve_index()
        elif path in STATIC_FILES:
            self._serve_static(path)
        elif path == "/api/latest":
            self._json(latest_record())
        elif path == "/api/current":
            self._json(get_current())
        elif path == '/api/catalog':
            self._json(get_catalog())
        elif path == '/api/config-root':
            if not self._check_post():return
            self._json(dict(ok=True,**CONFIG_ROOT.public()))
        elif path == '/api/releases':
            if not self._check_post():return
            self._json(speedbench_releases.local_info(_release_version()))
        elif path == '/api/desktop/identity':
            if not self._check_post():return
            self._json(DESKTOP_IDENTITY or {'ok':False,'msg':'Not a desktop backend'},200 if DESKTOP_IDENTITY else 404)
        elif path == '/api/preferences':
            if not self._check_post():return
            try:self._json({'ok':True,'version':1,'values':Preferences(DATA_HOME).read()})
            except PreferenceError:self._json({'ok':False,'msg':'无法读取偏好，请保留文件并恢复私有备份'},503)
        elif path == '/api/data-status':
            if not self._check_post():return
            try:
                # Existence only: never open raw history, migrate a database,
                # copy an identity seed or enumerate an arbitrary directory.
                home=DATA_HOME.resolve();history=HISTORY.resolve()
                database=HISTORY.with_suffix('.db').resolve()
                source_history=HERE/'speedbench-history.jsonl'
                source_database=HERE/'speedbench-history.db'
                alternate=(dict(path=str(source_history.parent.resolve()),
                                jsonl_exists=source_history.is_file(),
                                database_exists=source_database.is_file())
                           if source_history.parent.resolve()!=home else None)
                self._json(dict(ok=True,data_home=str(home),automatic_import=False,
                    history=dict(jsonl_path=str(history),jsonl_exists=history.is_file(),
                                 database_path=str(database),database_exists=database.is_file()),
                    alternate=alternate,
                    backup_names=['speedbench-history.jsonl','speedbench-history.db','ui-preferences.json','identity-seed']))
            except (OSError,RuntimeError,ValueError):self._json({'ok':False,'msg':'数据目录状态暂不可用；未修改任何文件'},503)
        elif path == '/api/history-import/status':
            if not self._check_post():return
            if self._data_busy():return
            try:
                with _DB_SYNC_LOCK:self._json(dict(ok=True,**history_transfer().status()))
            except (TransferError,OSError,ValueError):
                self._json({'ok':False,'msg':'私有导入记录暂不可用，请保留备份'},503)
        elif path == '/api/desktop/state':
            if not self._check_post():return
            if DESKTOP_ACTIONS is None:self._reject('not a desktop backend',404);return
            self._json({'notifications':DESKTOP_ACTIONS.notifications,'job':JOBS.summary(),'actions':DESKTOP_ACTIONS.drain()})
        elif re.fullmatch(r'/api/desktop/actions/[0-9a-f]{32}',path):
            if not self._check_post():return
            result=DESKTOP_ACTIONS.result(path.rsplit('/',1)[-1]) if DESKTOP_ACTIONS else None
            self._json(result or {'status':'unavailable'},200 if result else 404)
        elif path == '/api/sources/history':
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            sync_db()
            self._json(speedbench_db.source_summary(db_path(), days=_days_param(qs),
                subscription_id=qs.get('subscription_id', [None])[0]))
        elif path == '/api/source':
            qs=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            sync_db()
            self._json(speedbench_db.source_series(db_path(),qs.get('subscription_id',[''])[0],days=_days_param(qs)))
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
            node_id = qs.get('node_id', [''])[0]
            if not name and not key and not node_id:
                self._json({"ok": False, "msg": "缺少 name 参数"}, 400)
                return
            days = _days_param(qs)
            sync_db()
            self._json({
                "series": speedbench_db.node_series(db_path(), name, days=days,
                                                    node_key=key, node_id=node_id),
                "ip_changes": (speedbench_db.ip_changes(db_path(),name,node_id=node_id)
                               if name or node_id else []),
                "ip_reputation_changes": (speedbench_db.ip_reputation_changes(
                    db_path(), name, node_id=node_id) if node_id else
                    _load_ip_reputation_changes(name=name, node_key=key)),
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
                    'job_id':STATE.get('job_id'),
                    'job':JOBS.latest(),
                })
        elif path == '/api/jobs' or path.startswith('/api/jobs/'):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._job_get(path,qs)
        elif path == '/api/tasks':
            self._json(dict(version=1,tasks=speedbench_db.task_history(db_path())))
        elif re.fullmatch(r'/api/tasks/job_[0-9a-f]{32}',path):
            task = speedbench_db.task_snapshot(db_path(),path.rsplit('/',1)[1])
            self._json(task if task else {'error':'任务不存在'},200 if task else 404)
        elif path == '/api/task-config':
            self._json(dict(version=1, limits=speedbench_tasks.LIMITS,
                            modes={m:speedbench_tasks.resolve_config({'mode':m}).public()
                                   for m in speedbench_tasks.MODES}))
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
        if path in ('/api/history-import/preview','/api/history-import/apply','/api/history-import/rollback'):
            self._history_import(path)
        elif path in ('/api/preferences','/api/leak/audit','/api/leak/save','/api/leak/evaluate','/api/switch','/api/switch/preview'):
            if self._data_busy():return
            with _DB_SYNC_LOCK:
                if self._data_busy():return
                self._post(path)
        else:
            self._post(path)

    def _history_import(self, path):
        action = path.rsplit('/',1)[-1]
        key = {'preview':'directory','apply':'token','rollback':'backup_id'}[action]
        body = self._read_body(strict=True)
        if not isinstance(body,dict) or set(body)!={key} or not isinstance(body[key],str):
            self._json({'ok':False,'msg':'导入请求字段无效'},400);return
        with STATE_LOCK:
            if (STATE['running'] or STATE.get('cleanup_incomplete') or STATE.get('importing') or
                    STATE.get('import_failed') or DATA_OWNER is None or
                    (DESKTOP_EXITING is not None and DESKTOP_EXITING.is_set())):
                self._json({'ok':False,'msg':'目录未由本实例持有，或任务／导入／恢复仍在进行'},409);return
            STATE['importing']=True
        try:
            with _DB_SYNC_LOCK:
                service=history_transfer()
                if HISTORY.resolve()!=service.home/'speedbench-history.jsonl':
                    raise TransferError('历史位置不符合本实例固定数据目录')
                operation=getattr(service,action)
                self._json(operation(body[key],DATA_OWNER))
                if action!='preview':_DB_SYNCED.pop(str(db_path()),None)
        except TransferError as error:
            self._json({'ok':False,'msg':str(error)},409)
        except (OSError,ValueError,RuntimeError):
            self._json({'ok':False,'msg':'无法安全完成导入或恢复；请保留私有备份'},409)
        finally:
            with STATE_LOCK:
                STATE['importing']=False
                STATE['import_failed']=(DATA_HOME/IMPORT_PENDING).exists()

    def _post(self, path):
        if path in ('/api/config-root','/api/config-root/preview'):
            body=self._read_body(strict=True)
            if not isinstance(body,dict) or set(body)!={'root'}:
                self._json({'ok':False,'msg':'仅接受 root 目录字段'},400);return
            try:
                root=validate_root(body['root'])
                if path.endswith('/preview'):
                    self._json(dict(ok=True,path=root or None,mode='custom' if root else 'auto',connection_verified=False));return
                with STATE_LOCK:
                    if (STATE['running'] or STATE.get('cleanup_incomplete',False) or STATE.get('importing') or
                            STATE.get('import_failed') or (DESKTOP_EXITING is not None and DESKTOP_EXITING.is_set())):
                        self._json({'ok':False,'msg':'任务或清理仍在进行，不能更改配置目录'},409);return
                    choice=CONFIG_ROOT.apply(root)
                self._json(dict(ok=True,**choice))
            except ConfigRootError:
                self._json({'ok':False,'msg':'配置目录无效；请检查本机绝对路径及固定文件布局。原设置未变。'},400)
        elif path == '/api/releases/check':
            if self._read_body(strict=True) != {}:
                self._json({'ok':False,'msg':'版本检查不接受参数或凭据'},400);return
            self._json(RELEASE_CHECKER.check(_release_version()))
        elif path in ('/api/run','/api/jobs'):
            if DESKTOP_EXITING is not None and DESKTOP_EXITING.is_set():
                self._json({'ok':False,'msg':'客户端正在退出，不能开始新任务'},409);return
            with STATE_LOCK:
                busy = STATE["running"] or STATE.get('importing') or STATE.get('import_failed')
                cleanup_incomplete=STATE.get('cleanup_incomplete',False)
                clock_failed=STATE.get('power_clock_failed',False)
                root,root_revision=CONFIG_ROOT.snapshot()
            if cleanup_incomplete:
                self._reject('此前 worker 清理未完成；请退出并核对本任务残留资源后再重新启动，不会强行继续测速',409)
                return
            if clock_failed:
                self._reject(POWER_CLOCK_FAILED_MSG,409)
                return
            if busy:
                self._reject("已有测速任务进行中", 409)
                return
            params = self._read_body()
            if path == '/api/jobs' and isinstance(params,dict):
                params.setdefault('mode','standard')
            try:
                params = validate_run_params(params)
            except speedbench_tasks.TaskConfigError as e:
                self._json({'ok':False,'msg':str(e)},400)
                return
            if not _check_source_selection(params):
                self._json({'ok': False, 'msg': '来源/节点选择无效或未加载，请刷新目录'}, 400)
                return
            # Reserve ownership before dispatch. Otherwise simultaneous POSTs
            # can both observe idle before either benchmark thread starts.
            with STATE_LOCK:
                if CONFIG_ROOT.snapshot()[1]!=root_revision:
                    self._json({'ok':False,'msg':'配置目录已改变，请刷新目录后重试'},409);return
                if DESKTOP_EXITING is not None and DESKTOP_EXITING.is_set():
                    self._json({'ok':False,'msg':'客户端正在退出，不能开始新任务'},409);return
                if STATE['running'] or STATE.get('importing') or STATE.get('import_failed'):
                    self._reject('已有测速任务进行中',409)
                    return
                if STATE.get('cleanup_incomplete',False):
                    self._reject('此前 worker 清理未完成；不能接受新任务',409)
                    return
                if STATE.get('power_clock_failed',False):
                    self._reject(POWER_CLOCK_FAILED_MSG,409)
                    return
                try:
                    config = speedbench_tasks.resolve_config({k:v for k,v in params.items()
                        if k not in ('include','auto_switch','subscription_ids','node_ids','allow_serial')})
                    job_id = JOBS.create(config)
                    # Clear only this backend's known sentinel before ownership
                    # is dispatched. The child will never erase a fresh cancel.
                    CANCEL_FILE.unlink(missing_ok=True)
                except JobError:
                    self._reject('已有测速任务进行中',409)
                    return
                except OSError:
                    JOBS.transition(job_id,'failed')
                    self._json({'ok':False,'msg':'任务取消通道不可用'},500)
                    return
                # Arm the exact new job with a fresh native baseline inside the
                # same ownership hold, before the worker thread or any child
                # measurement can start. A broken clock fails closed here.
                if DESKTOP_POWER is not None:
                    try:
                        DESKTOP_POWER.arm(job_id)
                    except power.PowerClockError:
                        STATE['power_clock_failed']=True
                        JOBS.transition(job_id,'failed')
                        self._json({'ok':False,'msg':POWER_CLOCK_FAILED_MSG},500)
                        return
                params = dict(params,_job_id=job_id,_config_root=root)
                STATE.update(running=True, started=time.time(), exit_code=None, proc=None, lines=[],
                             job_id=job_id,cancel_requested=False,cancel_reason=None)
            try:
                threading.Thread(target=run_benchmark, args=(params,), daemon=True).start()
            except Exception:
                with STATE_LOCK:
                    STATE.update(running=False,exit_code=-1)
                if DESKTOP_POWER is not None:
                    DESKTOP_POWER.disarm(job_id)
                JOBS.transition(job_id,'failed')
                self._json({'ok':False,'msg':'测速任务启动失败'},500)
                return
            self._json({"ok": True,'version':1,'job_id':job_id},202 if path=='/api/jobs' else 200)
        elif path == "/api/switch/preview":
            body = self._read_body()
            if not isinstance(body, dict):
                self._json({'ok':False,'msg':'请求格式无效'},400)
                return
            name = body.get('name', '')
            node_id = body.get('node_id', '')
            if not isinstance(name, str) or not isinstance(node_id, str) or len(node_id) > 80:
                self._json({'ok':False,'msg':'节点身份无效'},400)
                return
            if not name and not node_id:
                self._json({'ok':False,'msg':'缺少节点名'},400)
                return
            self._json(preview_switch(name=name, node_id=node_id))
        elif path == "/api/switch":
            body = self._read_body()
            if not isinstance(body, dict):
                self._json({'ok':False,'msg':'请求格式无效'},400)
                return
            name = str(body.get("name", ""))
            node_id = body.get('node_id', '')
            confirmation = body.get('confirmation') if 'confirmation' in body else None
            if 'confirmation' in body and not _valid_switch_plan(confirmation):
                self._json({'ok':False,'msg':'确认信息无效，请刷新后重新确认切换'},400)
                return
            if confirmation is not None and node_id!=confirmation['node_id']:
                self._json({'ok':False,'msg':'确认目标不一致，请刷新后重新确认切换'},400)
                return
            if not isinstance(node_id, str) or len(node_id) > 80:
                self._json({'ok':False,'msg':'节点身份无效'},400)
                return
            if not name and not node_id and confirmation is None:
                self._json({"ok": False, "msg": "缺少节点名"}, 400)
                return
            if confirmation is not None:
                self._json(do_switch(name, node_id=node_id, confirmation=confirmation))
            else:
                self._json(do_switch(name, node_id=node_id) if node_id else do_switch(name))
        elif path == "/api/run/cancel":
            self._json(cancel_benchmark())
        elif re.fullmatch(r'/api/jobs/job_[0-9a-f]{32}/cancel',path):
            job_id = path.split('/')[3]
            with STATE_LOCK:
                matches = STATE.get('job_id') == job_id and STATE['running']
            if not matches:
                self._json({'ok':False,'msg':'不是当前活动任务'},409)
            else:
                self._json(cancel_benchmark(expected_job_id=job_id))
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
            audit=speedbench_leak.make_audit_record(evaluation, dns_status=dns_status)
            environment=body.get('client_environment') if isinstance(body,dict) else None
            audit['details']['client_environment']=environment if environment in ('browser','webview') else 'unknown'
            audit['details']['environment_reported_by_client']=True
            saved = _save_leak_audit(audit)
            result["persistence"] = saved
            self._json(result)
        elif path == '/api/preferences':
            body=self._read_body(strict=True)
            try:
                values=Preferences(DATA_HOME).patch(body)
                if DESKTOP_ACTIONS:DESKTOP_ACTIONS.notifications=values.get('sb_notifications')=='on'
                self._json({'ok':True})
            except PreferenceError:self._json({'ok':False,'msg':'偏好无效或无法安全保存'},400)
        elif path == '/api/desktop/actions':
            body=self._read_body()
            if DESKTOP_ACTIONS is None:self._reject('not a desktop backend',404);return
            try:self._json({'ok':True,'request_id':DESKTOP_ACTIONS.request(body)},202)
            except ValueError:self._json({'ok':False,'msg':'桌面操作无效或队列已满'},400)
        elif path == "/api/quit":
            if self._data_busy():return
            if callable(DESKTOP_SHUTDOWN):
                if DESKTOP_EXITING is not None:
                    with STATE_LOCK:DESKTOP_EXITING.set()
                self._json({'ok':True,'msg':'客户端正在取消任务并清理，完成后退出'})
                threading.Thread(target=DESKTOP_SHUTDOWN,daemon=True).start();return
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
    global DATA_OWNER
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
    try:
        with BackendLease(DATA_HOME) as lease:
            DATA_OWNER=lease
            try:return serve_web(args)
            finally:DATA_OWNER=None
    except LeaseError:
        print('SpeedBench 数据目录已被占用或无法安全锁定；请先关闭使用同一目录的现有面板。')
        return 2
    except TransferError:
        print('历史导入恢复未完成；请保留数据目录和 history-import-backups，停止新写入并检查私有备份。')
        return 2


def serve_web(args):
    recover_history_import()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        n = sync_db()  # 启动时先把 jsonl 历史增量入库（幂等）
        # Binding the singleton local port succeeded before marking old tasks.
        speedbench_db.interrupt_tasks(db_path())
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
        if hasattr(server,'server_close'):server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
