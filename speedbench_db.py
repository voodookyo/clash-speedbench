#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Clash SpeedBench 历史库（SQLite，零第三方依赖）。

speedbench-history.jsonl 仍是原始备份（只追加、不删除）；本模块把它镜像成
同目录的 speedbench-history.db，供 Web 面板走 SQL 查询：

- import_jsonl()  增量导入：按 runs.ts 去重，幂等，坏行跳过；
- latest_run()    最后一轮记录（结构与 jsonl 行完全一致，/api/latest 原样返回）；
- all_runs()      全部轮次（同结构列表，/api/history 用）；
- node_series()   某节点最近 N 天逐次测速的 带宽/延迟/抖动 序列（可按 name 或 node_key）；
- ip_changes()    某节点出口 IP / ASN 变化时间线；
- ip_reputation_changes() 出口 IP、分类、等级和第三方分数变化时间线；
- insert_leak_audit()/leak_audits() 保存/读取本地 WebRTC/DNS 审计结果；
- subscription_summary() / subscription_series()  订阅（provider）维度的聚合与逐轮趋势。

打开库时 _ensure_columns() 会给旧库就地补新列（node_key/fail_reason、region/city），
无需手工迁移。

结构保真的做法：runs.raw 直接存原始 jsonl 行文本，读取端 json.loads 回放，
因此旧行缺新字段、老 ip 结构（risk、kind="住宅" 等）都能原样通过，前端零改动；
node_results / ip_profiles / ip_intel_results 只是为查询建的索引表，缺失字段一律存 NULL。
Provider 的完整 raw response 不写入 runs.raw 或 ip_intel_results；付费 provider
缓存由 speedbench_ip_intel.IpIntelCache 管理，且同样使用 provider + IP 主键。
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL UNIQUE,        -- 测速时间（ISO 本地时间，与 jsonl 一致）
    mb         REAL,                        -- 每轮下载量 MB
    rounds     INTEGER,                     -- 轮数
    node_count INTEGER NOT NULL DEFAULT 0,  -- 本轮节点数
    raw        TEXT NOT NULL                -- 原始 jsonl 行：原样回放的底账
);
CREATE TABLE IF NOT EXISTS node_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    name        TEXT NOT NULL,
    proto       TEXT,
    provider    TEXT,
    node_key    TEXT,                       -- 节点稳定身份（proto|server|port 哈希）
    exit_ipv4   TEXT,
    exit_ipv6   TEXT,
    latency_ms  REAL,
    jitter_ms   REAL,
    connect_ms  REAL,
    median_mbps REAL,
    best_mbps   REAL,
    multi_mbps  REAL,
    sample_mb   REAL,
    score       REAL,
    stars       TEXT,
    status      TEXT,
    fail_reason TEXT,                       -- 失败原因分类（timeout/no_data/...）
    tags        TEXT,
    probe_attempts INTEGER,
    probe_successes INTEGER,
    probe_failures INTEGER,
    probe_success_rate REAL,
    probe_loss_pct REAL,
    network_score REAL,
    ip_quality_score REAL,
    ip_grade TEXT
);
CREATE INDEX IF NOT EXISTS idx_node_results_name ON node_results(name);
CREATE INDEX IF NOT EXISTS idx_node_results_provider ON node_results(provider);
CREATE TABLE IF NOT EXISTS ip_profiles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(id),
    name         TEXT NOT NULL,
    exit_ip      TEXT,
    country      TEXT,
    country_code TEXT,
    region       TEXT,
    city         TEXT,
    isp          TEXT,
    org          TEXT,
    asn          TEXT,
    asname       TEXT,
    kind         TEXT,
    ok           INTEGER,                   -- 1/0；旧格式无 ok 字段时按 exit_ip 推断
    proxy        INTEGER,                   -- 布尔存 0/1；旧格式没有这三个字段，存 NULL
    hosting      INTEGER,
    mobile       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ip_profiles_name ON ip_profiles(name);
CREATE TABLE IF NOT EXISTS ip_intel_cache (
    provider        TEXT NOT NULL,
    ip              TEXT NOT NULL,
    fetched_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    raw_json        TEXT NOT NULL,
    normalized_json TEXT NOT NULL,
    PRIMARY KEY (provider, ip)
);
CREATE INDEX IF NOT EXISTS idx_ip_intel_cache_ip ON ip_intel_cache(ip);
CREATE TABLE IF NOT EXISTS ip_intel_results (
    run_id               INTEGER NOT NULL REFERENCES runs(id),
    exit_ip              TEXT NOT NULL,
    ip_version           INTEGER,
    classification       TEXT NOT NULL DEFAULT 'unknown',
    confidence           INTEGER NOT NULL DEFAULT 0,
    ip_quality_score     REAL,
    ip_grade             TEXT,
    ipqs_fraud_score     INTEGER,
    scamalytics_score    INTEGER,
    evidence_json        TEXT NOT NULL DEFAULT '[]',
    conflicts_json       TEXT NOT NULL DEFAULT '[]',
    provider_status_json TEXT NOT NULL DEFAULT '{}',
    normalized_json      TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, exit_ip)
);
CREATE INDEX IF NOT EXISTS idx_ip_intel_results_exit_ip
    ON ip_intel_results(exit_ip);
CREATE TABLE IF NOT EXISTS leak_audits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    INTEGER NOT NULL,
    exit_ipv4     TEXT,
    exit_ipv6     TEXT,
    webrtc_status TEXT NOT NULL DEFAULT 'unknown',
    candidates_json TEXT NOT NULL DEFAULT '[]',
    dns_mode      TEXT NOT NULL DEFAULT 'guided',
    dns_status    TEXT,
    details_json  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_leak_audits_created_at
    ON leak_audits(created_at DESC);
CREATE TABLE IF NOT EXISTS subscription_sources (
    subscription_id TEXT PRIMARY KEY,
    current_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS node_identities (
    node_id TEXT PRIMARY KEY,
    identity_version INTEGER NOT NULL,
    identity_strength TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS node_origins (
    node_result_id INTEGER NOT NULL REFERENCES node_results(id),
    subscription_id TEXT NOT NULL REFERENCES subscription_sources(subscription_id),
    name_snapshot TEXT NOT NULL,
    source_status TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(node_result_id, subscription_id)
);
CREATE INDEX IF NOT EXISTS idx_node_origins_subscription ON node_origins(subscription_id);
CREATE TABLE IF NOT EXISTS task_runs (
    job_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    target_profile TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    config_json TEXT NOT NULL,
    run_id INTEGER REFERENCES runs(id),
    partial INTEGER NOT NULL DEFAULT 1,
    results_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_task_runs_started ON task_runs(started_at DESC);
CREATE TABLE IF NOT EXISTS task_metrics (
    job_id TEXT NOT NULL REFERENCES task_runs(job_id),
    phase TEXT NOT NULL,
    duration_ms REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    successes INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0,
    counters_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(job_id, phase)
);
"""

# 旧库就地升级时要补的列（新库的 SCHEMA 已包含，_ensure_columns 对其为 no-op）
_EXTRA_COLUMNS = {
    'runs': [('job_id','TEXT')],
    'task_runs': [('elapsed_ms','REAL')],
    "node_results": [
        ("node_key", "TEXT"), ("exit_ipv4", "TEXT"), ("exit_ipv6", "TEXT"),
        ("fail_reason", "TEXT"),
        ("probe_attempts", "INTEGER"), ("probe_successes", "INTEGER"),
        ("probe_failures", "INTEGER"), ("probe_success_rate", "REAL"),
        ("probe_loss_pct", "REAL"), ("network_score", "REAL"),
        ("ip_quality_score", "REAL"), ("ip_grade", "TEXT"),
        ("node_id", "TEXT"), ("identity_version", "INTEGER"),
        ("identity_strength", "TEXT"), ("source_status", "TEXT"),
    ],
    "ip_profiles": [("region", "TEXT"), ("city", "TEXT"), ('node_result_id','INTEGER')],
    # These entries make an interrupted/experimental migration repairable as
    # well.  Normal v0.8/v0.9 databases do not have the tables, so SCHEMA
    # creates them first and these ALTERs become no-ops.
    "ip_intel_results": [
        ("ip_version", "INTEGER"), ("classification", "TEXT"),
        ("confidence", "INTEGER"), ("ip_quality_score", "REAL"),
        ("ip_grade", "TEXT"), ("ipqs_fraud_score", "INTEGER"),
        ("scamalytics_score", "INTEGER"), ("evidence_json", "TEXT"),
        ("conflicts_json", "TEXT"), ("provider_status_json", "TEXT"),
        ("normalized_json", "TEXT"),
    ],
    "leak_audits": [
        ("created_at", "INTEGER"), ("exit_ipv4", "TEXT"),
        ("exit_ipv6", "TEXT"), ("webrtc_status", "TEXT"),
        ("candidates_json", "TEXT"), ("dns_mode", "TEXT"),
        ("dns_status", "TEXT"), ("details_json", "TEXT"),
    ],
}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """缺列的旧库用 ALTER TABLE 补上（SQLite 的 ADD COLUMN 不支持 IF NOT EXISTS，
    先查 PRAGMA table_info）。表名/列名/类型全部来自上方常量，无注入面。"""
    with conn:
        for table, cols in _EXTRA_COLUMNS.items():
            have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for col, ctype in cols:
                if col not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ctype}")
                    if table=='ip_profiles' and col=='node_result_id':
                        # A reused name is not sufficient identity evidence.
                        conn.execute('''UPDATE ip_profiles SET node_result_id=(
                            SELECT MIN(n.id) FROM node_results n
                            WHERE n.run_id=ip_profiles.run_id AND n.name=ip_profiles.name
                            HAVING COUNT(*)=1)''')


def _run_time(value):
    """Actual instant for ordering imported runs; old naive times are local.

    Invalid historical timestamps keep their raw text and IDs, sort before
    known instants, and remain excluded from dated trend windows.
    """
    try:
        if not isinstance(value, str) or len(value) > 64:
            return None
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _time_key(value):
    stamp = _run_time(value)
    return stamp if stamp is not None else float('-inf')


def _open(db_path) -> sqlite3.Connection:
    """打开（必要时创建）历史库并确保表结构存在。WAL：读查询不阻塞导入。"""
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.create_function('speedbench_run_time', 1, _run_time)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_node_results_identity ON node_results(node_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_runs_job ON runs(job_id)')
    return conn


def _bool_or_none(v):
    """布尔/缺失 → 1/0/NULL（旧格式没有 proxy/hosting/mobile 时保持 NULL）。"""
    return None if v is None else int(bool(v))


_SECRET_FIELD_RE = re.compile(
    r"(?:token|api[_-]?key|access[_-]?key|secret|password|authorization|credential|"
    r"(?:^|[_-])key$|username|user[_-]?name)",
    re.IGNORECASE,
)


def _known_secret_values():
    """Return configured credentials for a final persistence redaction pass.

    The database module does not need to know provider configuration to import
    historical rows.  It nevertheless reads the well-known environment names
    here so a malformed provider response or a locally supplied audit detail
    cannot accidentally place a credential in SQLite.  Values are used only in
    memory and are never returned by this module.
    """
    names = (
        "SPEEDBENCH_IPINFO_TOKEN", "SPEEDBENCH_IPQS_KEY",
        "SPEEDBENCH_SCAMALYTICS_USERNAME", "SPEEDBENCH_SCAMALYTICS_KEY",
    )
    return tuple(str(os.environ[name]) for name in names if os.environ.get(name))


def _safe_json_value(value, secrets=(), key=None):
    """JSON-safe local value with credential-shaped keys removed.

    This is intentionally independent from the provider module.  SQLite
    history import must continue to work when optional provider code is not
    importable, and parameterized SQL already protects all scalar values.
    """
    secret_values = tuple(str(item) for item in secrets if item)
    if key is not None and _SECRET_FIELD_RE.search(str(key)):
        return "[redacted]"
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str):
            text = value
            for secret in secret_values:
                text = text.replace(secret, "[redacted]")
            return text
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, dict):
        return {
            str(k): _safe_json_value(v, secret_values, str(k))
            for k, v in value.items()
            if not _SECRET_FIELD_RE.search(str(k))
        }
    if isinstance(value, (list, tuple, set)):
        return [_safe_json_value(v, secret_values) for v in value]
    # Dataclasses and custom objects should not be serialized implicitly into
    # a history database.  Their string representation may contain secrets.
    return "[unsupported]"


def _safe_json(value, default):
    """Serialize a redacted value deterministically for JSON columns."""
    safe = _safe_json_value(value, _known_secret_values())
    try:
        return json.dumps(safe, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        return default


_TASK_TERMINAL = ('completed', 'cancelled', 'failed', 'interrupted')


def save_task(db_path, snapshot):
    """Idempotent bounded checkpoint; never modifies legacy runs.raw.

    Only trusted public configuration/result fields survive. A delayed active
    checkpoint cannot turn a persisted terminal task back into a running task.
    """
    from contextlib import closing
    from dataclasses import fields
    from speedbench_tasks import TaskConfig
    from speedbench_jobs import _result, TRANSITIONS, MAX_RESULTS, METRIC_PHASES, safe_counters, safe_milestones
    job_id = snapshot.get('job_id')
    status = snapshot.get('status')
    if not isinstance(job_id,str) or not re.fullmatch(r'job_[0-9a-f]{32}',job_id):
        raise ValueError('Invalid task identity')
    if status not in set(TRANSITIONS) | set(_TASK_TERMINAL):
        raise ValueError('Invalid task status')
    config = snapshot.get('config',{})
    config = {f.name:config[f.name] for f in fields(TaskConfig) if f.name in config
              and (config[f.name] is None or isinstance(config[f.name],(str,int,float,bool)))}
    results = [_result(r) for r in snapshot.get('results',[])[:MAX_RESULTS]]
    with closing(_open(db_path)) as conn, conn:
        old = conn.execute('SELECT status FROM task_runs WHERE job_id=?',(job_id,)).fetchone()
        if old and old[0] in _TASK_TERMINAL:
            return False
        conn.execute('''INSERT INTO task_runs
            (job_id,mode,target_profile,status,started_at,finished_at,config_json,partial,results_json,elapsed_ms,run_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,(SELECT id FROM runs WHERE job_id=? ORDER BY id DESC LIMIT 1)) ON CONFLICT(job_id) DO UPDATE SET
            status=excluded.status, finished_at=excluded.finished_at,
            partial=excluded.partial, results_json=excluded.results_json,
            elapsed_ms=excluded.elapsed_ms,run_id=excluded.run_id''',
            (job_id, config.get('mode','legacy'),config.get('target_profile','balanced'),
             status,str(snapshot.get('started_at',''))[:64],
             str(snapshot['finished_at'])[:64] if snapshot.get('finished_at') else None,
             _safe_json(config,'{}'),int(status!='completed'),_safe_json(results,'[]'),
             max(0,min(_db_number(snapshot.get('elapsed_ms')) or 0,1e15)),job_id))
        for phase, metric in snapshot.get('metrics',{}).items():
            if phase not in METRIC_PHASES or not isinstance(metric,dict):
                continue
            def count(key):
                value = metric.get(key,0)
                return value if isinstance(value,int) and not isinstance(value,bool) and 0<=value<2**63 else 0
            duration = _db_number(metric.get('duration_ms',0)) or 0
            duration = max(0,min(duration,1e15))
            attempts = count('attempts')
            counters = safe_counters(metric.get('counters'))
            conn.execute('''INSERT INTO task_metrics VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(job_id,phase) DO UPDATE SET
                duration_ms=excluded.duration_ms,attempts=excluded.attempts,
                successes=excluded.successes,bytes=excluded.bytes,counters_json=excluded.counters_json''',
                (job_id,phase,duration,attempts,min(attempts,count('successes')),count('bytes'),_safe_json(counters,'{}')))
        milestones=safe_milestones(snapshot.get('milestones'))
        if milestones:
            previous = conn.execute(
                'SELECT counters_json FROM task_metrics WHERE job_id=? AND phase=?',
                (job_id,'milestones')).fetchone()
            if previous:
                try:
                    # First observations are immutable, even when a delayed
                    # active checkpoint arrives after a more recent snapshot.
                    milestones.update(safe_milestones(json.loads(previous[0])))
                except (TypeError,ValueError):
                    pass
            conn.execute('''INSERT INTO task_metrics VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(job_id,phase) DO UPDATE SET counters_json=excluded.counters_json''',
                (job_id,'milestones',0,0,0,0,_safe_json(milestones,'{}')))
    return True


def _task_row(row):
    return dict(version=1,job_id=row[0],mode=row[1],target_profile=row[2],status=row[3],
                started_at=row[4],finished_at=row[5],config=json.loads(row[6]),
                run_id=row[7],partial=bool(row[8]),elapsed_ms=row[10])


def task_history(db_path, limit=100):
    from contextlib import closing
    with closing(_open(db_path)) as conn:
        return [_task_row(r) for r in conn.execute(
            'SELECT * FROM task_runs ORDER BY speedbench_run_time(started_at) DESC,job_id DESC LIMIT ?', (max(1,min(int(limit),1000)),))]


def task_snapshot(db_path, job_id):
    from contextlib import closing
    with closing(_open(db_path)) as conn:
        row = conn.execute('SELECT * FROM task_runs WHERE job_id=?',(job_id,)).fetchone()
        if not row:
            return None
        result = _task_row(row)
        result['results'] = json.loads(row[9])
        result['metrics'] = {r[0]:dict(duration_ms=r[1],attempts=r[2],successes=r[3],bytes=r[4],
                                     counters=json.loads(r[5])) for r in conn.execute(
                                         'SELECT phase,duration_ms,attempts,successes,bytes,counters_json FROM task_metrics WHERE job_id=?',
                                         (job_id,))}
        from speedbench_jobs import safe_milestones
        result['milestones']=safe_milestones(result['metrics'].pop('milestones',{}).get('counters'))
        return result


def interrupt_tasks(db_path):
    """Called only by an owning backend on restart; retains partial records."""
    from contextlib import closing
    with closing(_open(db_path)) as conn, conn:
        return conn.execute('''UPDATE task_runs SET status='interrupted',partial=1,finished_at=?
            WHERE status NOT IN ('completed','cancelled','failed','interrupted')''',
            (datetime.now(timezone.utc).isoformat(timespec='milliseconds'),)).rowcount


def _db_number(value):
    """Coerce JSON numeric fields to SQLite scalars; malformed values become NULL."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _db_int(value):
    number = _db_number(value)
    if number is None:
        return None
    try:
        return int(number)
    except (TypeError, ValueError, OverflowError):
        return None


def _db_text(value, default=None):
    """Coerce a scalar JSON value to text without binding mappings/lists."""
    if value is None:
        return default
    if isinstance(value, (dict, list, tuple, set)):
        return default
    text = str(value).strip()
    return text if text else default


def _family_ip(result, version):
    """Find an exit IP from a new or legacy result shape."""
    ip = result.get("ip") if isinstance(result, dict) else None
    keys = ("exit_ipv4", "ipv4", "exit_ip_v4", "exit_ip") if version == 4 else (
        "exit_ipv6", "ipv6", "exit_ip_v6")
    for source in (result, ip):
        if not isinstance(source, dict):
            continue
        for key in keys:
            text = _db_text(source.get(key))
            if text:
                return text
    return None


def _intel_candidates(result):
    """Yield ``(family, object)`` from tolerated result field spellings.

    Integrators used both ``intel_v4`` and ``ip_intel_v4`` during the staged
    rollout.  Accepting both here keeps historical imports independent of the
    exact serializer version.  Wrapper objects are unwrapped, while raw
    provider payloads are never persisted as-is.
    """
    if not isinstance(result, dict):
        return []
    out = []
    seen = set()
    for version, names in (
        (4, ("intel_v4", "ip_intel_v4")),
        (6, ("intel_v6", "ip_intel_v6")),
    ):
        for name in names:
            value = result.get(name)
            if not isinstance(value, dict):
                continue
            # A coordinator may wrap the unified object in ``intelligence``.
            if isinstance(value.get("intelligence"), dict):
                value = value["intelligence"]
            elif isinstance(value.get("intel"), dict):
                value = value["intel"]
            ip = _db_text(value.get("ip") or value.get("exit_ip"))
            if not ip:
                ip = _family_ip(result, version)
            marker = (version, ip or name)
            if marker in seen:
                continue
            seen.add(marker)
            out.append((version, value, ip))
    return out


def _classification_fields(value):
    classification = value.get("classification")
    if isinstance(classification, dict):
        category = (classification.get("category") or
                    classification.get("classification") or "unknown")
        confidence = classification.get("confidence", value.get("confidence", 0))
        evidence = classification.get("evidence", value.get("evidence", []))
        conflicts = classification.get("conflicts", value.get("conflicts", []))
    else:
        category = classification or value.get("category") or "unknown"
        confidence = value.get("confidence", 0)
        evidence = value.get("evidence", [])
        conflicts = value.get("conflicts", [])
    return (
        _db_text(category, "unknown"),
        _db_int(confidence) if _db_int(confidence) is not None else 0,
        evidence if isinstance(evidence, (list, tuple)) else [],
        conflicts if isinstance(conflicts, (list, tuple)) else [],
    )


def _intel_summary(value, version, ip):
    """Select only the public normalized summary allowed in history.

    In particular this drops provider ``raw``, ``provider_data`` and
    ``provider_results`` fields even if a caller accidentally includes them in
    the JSONL result.  The raw provider response belongs only in the cache
    module, which independently redacts it.
    """
    if not isinstance(value, dict):
        value = {}
    category, confidence, evidence, conflicts = _classification_fields(value)
    score = value.get("ip_quality_score")
    if score is None:
        score = value.get("quality_score")
    grade = value.get("ip_grade")
    if grade is None:
        grade = value.get("grade")
    status = value.get("provider_status", {})
    if not isinstance(status, dict):
        status = {}
    # Provider status is a small UI-facing map, never a response object.
    status = {
        _db_text(k, "provider"): _db_text(v, "error")
        for k, v in status.items()
        if _db_text(k) and _db_text(v)
    }
    allowed = {
        "ip": ip,
        "ip_version": _db_int(value.get("ip_version")) or version,
        "country": _db_text(value.get("country")),
        "asn": _db_text(value.get("asn")),
        "as_name": _db_text(value.get("as_name") or value.get("asname")),
        "isp": _db_text(value.get("isp")),
        "organization": _db_text(value.get("organization") or value.get("org")),
        "hosting": value.get("hosting") if isinstance(value.get("hosting"), bool) else None,
        "proxy": value.get("proxy") if isinstance(value.get("proxy"), bool) else None,
        "vpn": value.get("vpn") if isinstance(value.get("vpn"), bool) else None,
        "tor": value.get("tor") if isinstance(value.get("tor"), bool) else None,
        "mobile": value.get("mobile") if isinstance(value.get("mobile"), bool) else None,
        "residential_proxy": (
            value.get("residential_proxy")
            if isinstance(value.get("residential_proxy"), bool) else None
        ),
        "connection_type": _db_text(value.get("connection_type")),
        "ipqs_fraud_score": _db_int(value.get("ipqs_fraud_score"))
        if value.get("ipqs_fraud_score") is not None else _db_int(value.get("fraud_score")),
        "ipqs_recent_abuse": (
            value.get("ipqs_recent_abuse")
            if isinstance(value.get("ipqs_recent_abuse"), bool)
            else value.get("recent_abuse") if isinstance(value.get("recent_abuse"), bool)
            else None
        ),
        "ipqs_abuse_velocity": _db_text(value.get("ipqs_abuse_velocity") or value.get("abuse_velocity")),
        "scamalytics_score": _db_int(value.get("scamalytics_score")),
        "scamalytics_risk": _db_text(value.get("scamalytics_risk")),
        "scamalytics_datacenter": (
            value.get("scamalytics_datacenter")
            if isinstance(value.get("scamalytics_datacenter"), bool)
            else None
        ),
        "scamalytics_blacklisted": (
            value.get("scamalytics_blacklisted")
            if isinstance(value.get("scamalytics_blacklisted"), bool)
            else None
        ),
        "classification": category,
        "confidence": confidence,
        "ip_quality_score": _db_number(score),
        "ip_grade": _db_text(grade),
        "evidence": list(evidence),
        "conflicts": list(conflicts),
        "provider_status": status,
    }
    return _safe_json_value(allowed, _known_secret_values())


def _insert_ip_intel(conn, run_id, result):
    """Insert one normalized intel row per ``(run_id, exit_ip)``."""
    for version, value, ip in _intel_candidates(result):
        if not ip:
            continue
        summary = _intel_summary(value, version, ip)
        evidence = summary.get("evidence", [])
        conflicts = summary.get("conflicts", [])
        status = summary.get("provider_status", {})
        conn.execute(
            "INSERT OR IGNORE INTO ip_intel_results("
            "run_id, exit_ip, ip_version, classification, confidence, "
            "ip_quality_score, ip_grade, ipqs_fraud_score, scamalytics_score, "
            "evidence_json, conflicts_json, provider_status_json, normalized_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, ip, summary.get("ip_version") or version,
                summary.get("classification") or "unknown",
                summary.get("confidence") or 0,
                summary.get("ip_quality_score"), summary.get("ip_grade"),
                summary.get("ipqs_fraud_score"), summary.get("scamalytics_score"),
                _safe_json(evidence, "[]"), _safe_json(conflicts, "[]"),
                _safe_json(status, "{}"), _safe_json(summary, "{}"),
            ),
        )


def _insert_source_origins(conn, node_result_id, run_id, r):
    node_id = r.get('node_id')
    if (not isinstance(node_id, str) or not re.fullmatch(r'node_v2_[0-9a-f]{32}', node_id)
            or r.get('identity_version') != 2):
        return
    strength = 'strong' if r.get('identity_strength') == 'strong' else 'weak'
    status = r.get('source_status')
    status = status if status in ('verified', 'ambiguous', 'unknown') else 'unknown'
    ts = conn.execute('SELECT ts FROM runs WHERE id=?', (run_id,)).fetchone()[0]
    conn.execute('UPDATE node_results SET node_id=?,identity_version=2,identity_strength=?,source_status=? WHERE id=?',
                 (node_id, strength, status, node_result_id))
    conn.execute('INSERT INTO node_identities VALUES (?,2,?,?,?) ON CONFLICT(node_id) DO UPDATE SET last_seen=max(last_seen,excluded.last_seen),first_seen=min(first_seen,excluded.first_seen)',
                 (node_id, strength, ts, ts))
    memberships = r.get('subscriptions')
    if status == 'unknown' or not isinstance(memberships, list):
        return
    allowed_ids = r.get('subscription_ids')
    allowed_ids = allowed_ids if isinstance(allowed_ids, list) else []
    for membership in memberships[:1000]:
        if not isinstance(membership, dict):
            continue
        source_id = membership.get('subscription_id')
        if (not isinstance(source_id, str) or
                not re.fullmatch(r'subscription_v2_[0-9a-f]{32}', source_id) or
                source_id not in allowed_ids):
            continue
        name = _db_text(membership.get('name'), '未命名订阅')
        kind = membership.get('kind')
        kind = kind if kind in ('remote','local','provider','unknown') else 'unknown'
        conn.execute('INSERT INTO subscription_sources VALUES (?,?,?,?,?) ON CONFLICT(subscription_id) DO UPDATE SET current_name=CASE WHEN excluded.last_seen>=last_seen THEN excluded.current_name ELSE current_name END,last_seen=max(last_seen,excluded.last_seen),first_seen=min(first_seen,excluded.first_seen)',
                     (source_id, name, kind, ts, ts))
        # Source evidence contains fixed diagnostics, never raw configuration.
        evidence = ['Exact local connection definition match']
        conn.execute('INSERT OR IGNORE INTO node_origins VALUES (?,?,?,?,?)',
                     (node_result_id, source_id, name, status, json.dumps(evidence)))


def _insert_result(conn: sqlite3.Connection, run_id: int, r: dict) -> None:
    name = str(r.get("name") or "")
    # A staged/new serializer may include only ``intel_v4``/``intel_v6`` and
    # omit both the top-level family fields and the legacy ``ip`` object.  Use
    # those normalized IPs as a fallback so multi-node runs do not lose their
    # per-node exit identity in node_results/history.
    intel_candidates = _intel_candidates(r)
    candidate_ips = {}
    for version, _value, candidate_ip in intel_candidates:
        if candidate_ip and version not in candidate_ips:
            candidate_ips[version] = candidate_ip
    exit_ipv4 = _family_ip(r, 4) or candidate_ips.get(4)
    exit_ipv6 = _family_ip(r, 6) or candidate_ips.get(6)
    # 旧行缺 node_key/fail_reason/provider 等新字段时落 ""，保持可聚合
    inserted = conn.execute(
        "INSERT INTO node_results(run_id, name, proto, provider, node_key,"
        " latency_ms, jitter_ms, connect_ms, median_mbps, best_mbps, multi_mbps,"
        " sample_mb, score, stars, status, fail_reason, tags)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, name, _db_text(r.get("proto")), _db_text(r.get("provider"), ""),
         _db_text(r.get("node_key"), ""),
         _db_number(r.get("latency_ms")), _db_number(r.get("jitter_ms")),
         _db_number(r.get("connect_ms")), _db_number(r.get("median_mbps")),
         _db_number(r.get("best_mbps")), _db_number(r.get("multi_mbps")),
         _db_number(r.get("sample_mb")), _db_number(r.get("score")),
         _db_text(r.get("stars")), _db_text(r.get("status")),
         _db_text(r.get("fail_reason"), ""), _db_text(r.get("tags"))))
    # These fields are deliberately additive: old JSONL rows produce NULL and
    # remain byte-for-byte available through runs.raw.
    conn.execute(
        "UPDATE node_results SET probe_attempts=?, probe_successes=?, "
        "probe_failures=?, probe_success_rate=?, probe_loss_pct=?, "
        "network_score=?, ip_quality_score=?, ip_grade=?, exit_ipv4=?, exit_ipv6=? "
        "WHERE id = last_insert_rowid()",
        (_db_int(r.get("probe_attempts")), _db_int(r.get("probe_successes")),
         _db_int(r.get("probe_failures")), _db_number(r.get("probe_success_rate")),
         _db_number(r.get("probe_loss_pct")), _db_number(r.get("network_score")),
         _db_number(r.get("ip_quality_score")), _db_text(r.get("ip_grade") or r.get("grade")),
          exit_ipv4, exit_ipv6))
    _insert_source_origins(conn, inserted.lastrowid, run_id, r)
    ip = r.get("ip")
    if isinstance(ip, dict) and ip:
        # 旧格式（v0.2 及更早）的 ip 没有 ok 字段：按 exit_ip 是否存在推断查询成功
        ok = ip.get("ok")
        if ok is None:
            ok = bool(ip.get("exit_ip") or exit_ipv4)
        conn.execute(
            "INSERT INTO ip_profiles(run_id, name, exit_ip, country, country_code,"
            " region, city, isp, org, asn, asname, kind, ok, proxy, hosting, mobile)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, name, _db_text(ip.get("exit_ip") or exit_ipv4),
             _db_text(ip.get("country")),
             _db_text(ip.get("country_code")), _db_text(ip.get("region")),
             _db_text(ip.get("city")), _db_text(ip.get("isp")),
             _db_text(ip.get("org")), _db_text(ip.get("asn")),
             _db_text(ip.get("asname")), _db_text(ip.get("kind")), int(bool(ok)),
             _bool_or_none(ip.get("proxy")), _bool_or_none(ip.get("hosting")),
             _bool_or_none(ip.get("mobile"))))
    elif exit_ipv4:
        # A new serializer may place all basic data in intel_v4 and omit the
        # legacy ip object.  Create a minimal profile solely to keep the old
        # IP/ASN timeline useful; risk/provider payloads stay in the new table.
        basic = next((v for version, v, value in _intel_candidates(r)
                      if version == 4 and value), {})
        conn.execute(
            "INSERT INTO ip_profiles(run_id, name, exit_ip, country, country_code,"
            " region, city, isp, org, asn, asname, kind, ok, proxy, hosting, mobile)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, name, exit_ipv4, _db_text(basic.get("country")),
             _db_text(basic.get("country_code")), _db_text(basic.get("region")),
             _db_text(basic.get("city")), _db_text(basic.get("isp")),
             _db_text(basic.get("organization") or basic.get("org")),
             _db_text(basic.get("asn")),
             _db_text(basic.get("as_name") or basic.get("asname")),
             _db_text(basic.get("kind")), 1,
             _bool_or_none(basic.get("proxy")), _bool_or_none(basic.get("hosting")),
             _bool_or_none(basic.get("mobile"))))
    if (isinstance(ip,dict) and ip) or exit_ipv4:
        conn.execute('UPDATE ip_profiles SET node_result_id=? WHERE id=last_insert_rowid()',
                     (inserted.lastrowid,))
    # New family-specific intelligence may be present even when the legacy
    # ``ip`` object is absent.  Keep it independent from ip_profiles so a
    # partially populated result is still useful for the reputation timeline.
    _insert_ip_intel(conn, run_id, r)


def import_jsonl(db_path, jsonl_path) -> int:
    """把 jsonl 历史增量导入 SQLite，返回新导入的轮次数。

    幂等关键：按 runs.ts 去重，重复执行不产生重复数据；
    坏行（非 JSON / 非对象 / 缺 ts）跳过。jsonl 是只追加的小文件，
    全量扫一遍再滤重足够快，无需记录读取偏移量。
    """
    jsonl_path = Path(jsonl_path)
    conn = _open(db_path)
    try:
        known = {row[0] for row in conn.execute("SELECT ts FROM runs")}
        pending = []  # (记录 dict, 原始行文本)
        if jsonl_path.exists():
            with jsonl_path.open("r", encoding="utf-8") as f:
                for raw_line in f:
                    # Keep the JSON text exactly as written (apart from the
                    # physical line ending).  Parsing uses a trimmed view so
                    # blank/indented lines remain harmless, while runs.raw
                    # stays the original JSONL source of truth.
                    line = raw_line.rstrip("\r\n")
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # 坏行跳过
                    if not isinstance(rec, dict):
                        continue
                    ts = rec.get("ts")
                    if not ts or ts in known:
                        continue  # 同一 ts 只入一次
                    known.add(ts)
                    pending.append((rec, line))
        imported = 0
        with conn:  # 单事务：整批要么全进要么全不进
            for rec, line in pending:
                results = rec.get("results")
                results = results if isinstance(results, list) else []
                cur = conn.execute(
                    "INSERT OR IGNORE INTO runs(ts, mb, rounds, node_count, raw)"
                    " VALUES (?,?,?,?,?)",
                    (rec.get("ts"), rec.get("mb"), rec.get("rounds"),
                     len(results), line))
                if cur.rowcount == 0:
                    continue  # 并发下 ts 已被其他连接写入：跳过（仍幂等）
                run_id = cur.lastrowid
                job_id = rec.get('task',{}).get('job_id') if isinstance(rec.get('task'),dict) else None
                if isinstance(job_id,str) and re.fullmatch(r'job_[0-9a-f]{32}',job_id):
                    conn.execute('UPDATE runs SET job_id=? WHERE id=?',(job_id,run_id))
                for r in results:
                    if isinstance(r, dict):
                        _insert_result(conn, run_id, r)
                imported += 1
        return imported
    finally:
        conn.close()


def latest_run(db_path) -> dict:
    """最后一轮测速记录（与 jsonl 行结构完全一致）；无记录返回 {}。"""
    conn = _open(db_path)
    try:
        row = conn.execute(
            "SELECT raw FROM runs ORDER BY speedbench_run_time(ts) DESC,id DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else {}
    finally:
        conn.close()


def all_runs(db_path) -> list:
    """全部测速轮次（实际时间升序，同一时刻按 ID，raw 与旧 ID 不变）。"""
    conn = _open(db_path)
    try:
        return [json.loads(row[0])
                for row in conn.execute("SELECT raw FROM runs ORDER BY speedbench_run_time(ts),id")]
    finally:
        conn.close()


def _candidate_ip_hint(rows, exits):
    """Conservative public history hints, including the worse observed family."""
    qualities=[];categories=[];confidences=[];seen=set()
    ranks={'residential':7,'corporate':6,'mobile':5.5,'residential_proxy':5,
           'datacenter':3.5,'vpn_proxy':2,'unknown':0}
    grades={'S':95,'A':85,'B':70,'C':50,'D':20}
    def number(value):
        return float(value) if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and 0<=value<=100 else None
    for address,quality,grade,category,confidence,fraud,scam,conflicts,normalized in rows:
        seen.add(address)
        values=[v for v in (number(quality),grades.get(grade)) if v is not None]
        values.extend(100-v for v in (number(fraud),number(scam)) if v is not None)
        observed=bool(values)
        confidence=number(confidence)
        if confidence is not None:values.append(confidence)
        if category not in ranks:category='unknown'
        if category=='unknown' or conflicts:values.append(35)
        try:data=json.loads(normalized)
        except (ValueError,TypeError):data=None
        if not isinstance(data,dict):values.append(35)
        else:
            for key,cap in (('tor',10),('vpn',20),('proxy',25),('hosting',45),('mobile',75),
                            ('residential_proxy',55),('ipqs_recent_abuse',30),
                            ('scamalytics_blacklisted',20),('scamalytics_datacenter',45)):
                if data.get(key) is True:values.append(cap);observed=True
        qualities.append(min(values) if observed else 0)
        if not observed:category='unknown'
        categories.append(category);confidences.append(confidence or 0)
    if any(address and address not in seen for address in exits):
        qualities.append(0);categories.append('unknown');confidences.append(0)
    if not qualities:return {}
    return dict(recent_ip_quality=min(qualities),
                recent_ip_category=min(categories,key=lambda value:ranks[value]),
                recent_ip_confidence=min(confidences))


def candidate_history_hints(db_path, node_ids, *, now=None, target_profile='balanced') -> dict:
    """Bounded read-only hints for this task's strong stable-ID scope.

    Never creates/migrates a database, imports raw history, matches a name or
    returns a previous measurement as a present result. Missing/old/busy
    databases silently fall back to current probes. Seven-day successful
    single-stream samples or target-specific conservative IP summaries and
    validated country codes are selection hints
    only; country/exit reputations may have changed since that observation.
    """
    ids = sorted({v for v in list(node_ids)[:3000] if isinstance(v, str)
                  and re.fullmatch(r'node_v2_[0-9a-f]{32}', v)})
    if not ids:
        return {}
    conn = None
    hints = {}
    deadline = time.monotonic() + 0.25
    observed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        path = Path(db_path)
        if not path.is_file():
            return {}
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.05)
        conn.create_function('speedbench_run_time', 1, _run_time)
        conn.execute('PRAGMA query_only=ON')
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        # One indexed lookup per ID, limited to the newest usable bandwidth
        # observation. A newer probe-only or failed run is not a speed sample.
        sql = '''SELECT n.median_mbps,r.ts,
                 (SELECT p.country_code FROM ip_profiles p
                  WHERE p.node_result_id=n.id AND p.ok=1 ORDER BY p.id DESC LIMIT 1)
                 FROM node_results n JOIN runs r ON r.id=n.run_id
                 WHERE n.node_id=? AND n.identity_version=2
                 AND n.identity_strength='strong' AND n.status='ok'
                 AND n.median_mbps>0 ORDER BY speedbench_run_time(r.ts) DESC,n.id DESC LIMIT 1'''
        ip_target=target_profile in ('ip','residential')
        if ip_target:
            sql='''SELECT n.run_id,r.ts,n.exit_ipv4,n.exit_ipv6,
                   (SELECT p.country_code FROM ip_profiles p WHERE p.node_result_id=n.id
                    AND p.ok=1 ORDER BY p.id DESC LIMIT 1)
                   FROM node_results n JOIN runs r ON r.id=n.run_id
                   WHERE n.node_id=? AND n.identity_version=2 AND n.identity_strength='strong'
                   AND n.status='ok' AND EXISTS(SELECT 1 FROM ip_intel_results i
                     WHERE i.run_id=n.run_id AND i.exit_ip IN (n.exit_ipv4,n.exit_ipv6))
                   ORDER BY speedbench_run_time(r.ts) DESC,n.id DESC LIMIT 1'''
        for node_id in ids:
            if time.monotonic() >= deadline:
                # Do not bias a large task towards the first sorted IDs when
                # its optional history budget expires halfway through.
                return {}
            row = conn.execute(sql, (node_id,)).fetchone()
            if not row:
                continue
            if ip_target:run_id,stamp,ipv4,ipv6,region=row
            else:speed,stamp,region=row
            try:
                date = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                # Historical CLI timestamps without an offset are local time.
                age = (observed_now - date.astimezone(timezone.utc)).total_seconds() / 86400
                if not 0<=age<=7:continue
                if not ip_target and (not isinstance(speed,(int,float)) or not math.isfinite(speed) or speed<=0):continue
            except (ValueError, TypeError, AttributeError, OverflowError, OSError):
                continue
            hint=dict(history_age_days=age)
            if ip_target:
                rows=conn.execute('''SELECT exit_ip,ip_quality_score,ip_grade,classification,confidence,
                    ipqs_fraud_score,scamalytics_score,CASE WHEN conflicts_json='[]' THEN 0 ELSE 1 END,
                    substr(normalized_json,1,65536) FROM ip_intel_results
                    WHERE run_id=? AND exit_ip IN (?,?)''',(run_id,ipv4,ipv6)).fetchall()
                hint.update(_candidate_ip_hint(rows,(ipv4,ipv6)))
            else:hint['recent_mbps']=speed
            if isinstance(region, str) and re.fullmatch(r'[A-Z]{2}', region):
                hint['region'] = region
            hints[node_id] = hint
        if time.monotonic()>=deadline:return {}
    except (sqlite3.Error, OSError, ValueError):
        # Optional enrichment cannot make a task fail or leak a local path,
        # corrupt database content, credentials or raw exception in logs.
        return {}
    finally:
        if conn is not None:
            conn.close()
    return hints


def node_series(db_path, name: str, days: int = 30, node_key: str = "", node_id: str = "") -> list:
    """某节点最近 days 天逐次测速序列（时间升序）。

    带时区和无时区旧时间都按实际时刻排序；无时区按本机时区解释。
    node_key 非空时改按 node_key 匹配（订阅改名后仍可续上历史），否则按 name。
    """
    days = max(1, min(int(days), 3650))
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    if node_id:
        where, params = 'n.node_id = ?', (node_id, since)
    elif node_key:
        where, params = "n.node_key = ?", (node_key, since)
    else:
        where, params = "n.name = ?", (name, since)
    conn = _open(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT r.ts, n.median_mbps, n.best_mbps, n.multi_mbps,"
            " n.latency_ms, n.jitter_ms, n.connect_ms, n.score, n.status"
            " FROM node_results n JOIN runs r ON r.id = n.run_id"
            f" WHERE {where} AND speedbench_run_time(r.ts) >= speedbench_run_time(?)"
            " ORDER BY speedbench_run_time(r.ts),r.id,n.id",
            params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def ip_changes(db_path, name: str, node_id: str = '') -> list:
    """某节点出口 IP / ASN 变化时间线（时间升序）。

    相邻两次测速 (exit_ip, asn) 不变则合并，只保留变化点；
    查询失败（无 exit_ip）的轮次不参与——查不到不等于换了 IP。
    """
    conn = _open(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT r.ts, p.exit_ip, p.country, p.country_code, p.isp, p.org,"
            " p.asn, p.asname, p.kind, p.proxy, p.hosting, p.mobile"
            " FROM ip_profiles p JOIN runs r ON r.id = p.run_id"
            " WHERE p.name = ? AND p.exit_ip IS NOT NULL AND p.exit_ip != ''"
            " ORDER BY speedbench_run_time(r.ts),r.id,p.id",
            (name,)).fetchall()
        if node_id:
            rows=conn.execute('''SELECT r.ts,p.exit_ip,p.country,p.country_code,p.isp,p.org,
                p.asn,p.asname,p.kind,p.proxy,p.hosting,p.mobile FROM ip_profiles p
                JOIN runs r ON r.id=p.run_id JOIN node_results n ON n.id=p.node_result_id
                WHERE n.node_id=? AND p.exit_ip IS NOT NULL AND p.exit_ip!=''
                ORDER BY speedbench_run_time(r.ts),r.id,p.id''',(node_id,)).fetchall()
        timeline = []
        last_key = None
        for row in rows:
            d = dict(row)
            key = (d["exit_ip"], d["asn"])
            if key == last_key:
                continue
            last_key = key
            for k in ("proxy", "hosting", "mobile"):
                if d[k] is not None:
                    d[k] = bool(d[k])
            timeline.append(d)
        return timeline
    finally:
        conn.close()


def _json_column(value, default):
    """Decode a JSON column defensively for old/hand-edited databases."""
    if not isinstance(value, str):
        return default
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return default
    # Also sanitize on read.  This protects API callers when a database was
    # copied from an older build or edited outside the insertion helpers.
    return _safe_json_value(decoded, _known_secret_values())


def _risk_grade_rank(value):
    """Return a numeric rank where a smaller value is a worse grade."""
    ranks = {"D": 0, "C": 1, "B": 2, "A": 3, "S": 4}
    return ranks.get(str(value or "").upper())


def _reputation_worsened(previous, current):
    """Detect a conservative same-IP reputation deterioration.

    Vendor scores remain separate.  This helper only marks an obvious change:
    a lower SpeedBench grade, a category moving to a proxy/risk class, or a
    material increase in either vendor score (15 points).  Missing values do
    not count as a deterioration.
    """
    if not previous or not current or previous.get("exit_ip") != current.get("exit_ip"):
        return False
    old_rank = _risk_grade_rank(previous.get("ip_grade"))
    new_rank = _risk_grade_rank(current.get("ip_grade"))
    if old_rank is not None and new_rank is not None and new_rank < old_rank:
        return True
    old_category = str(previous.get("classification") or "unknown")
    new_category = str(current.get("classification") or "unknown")
    if new_category in {"residential_proxy", "vpn_proxy", "datacenter"} and \
            old_category not in {new_category, "vpn_proxy", "datacenter"}:
        return True
    for key in ("ipqs_fraud_score", "scamalytics_score"):
        old_score, new_score = previous.get(key), current.get(key)
        if old_score is not None and new_score is not None:
            try:
                if float(new_score) - float(old_score) >= 15:
                    return True
            except (TypeError, ValueError):
                pass
    return False


def ip_reputation_changes(db_path, name: str, node_key: str = "", node_id: str = "") -> list:
    """Return the deduplicated IP/reputation timeline for one node.

    The legacy ``ip_changes`` API intentionally keeps its old, small shape.
    This richer API joins node identity, basic ``ip_profiles`` and the new
    per-run/per-exit-IP intelligence rows.  Older runs with no intelligence
    therefore still contribute their IP/ASN entries with ``intel_available``
    false instead of being discarded or triggering provider calls.
    """
    conn = _open(db_path)
    try:
        conn.row_factory = sqlite3.Row
        if node_id:
            where, params = 'n.node_id = ?', (node_id,)
        elif node_key:
            where, params = "n.node_key = ?", (node_key,)
        else:
            where, params = "n.name = ?", (name,)
        rows = conn.execute(
            "SELECT r.id AS run_id, r.node_count AS run_node_count, r.ts, n.id AS node_result_id, n.name, "
            "n.node_key, n.exit_ipv4, n.exit_ipv6, "
            "p.exit_ip, p.country, p.country_code, p.isp, p.org, p.asn, "
            "p.asname, p.kind, p.proxy, p.hosting, p.mobile "
            "FROM node_results n JOIN runs r ON r.id = n.run_id "
            "LEFT JOIN ip_profiles p ON p.id = ("
            "SELECT p2.id FROM ip_profiles p2 WHERE p2.node_result_id=n.id "
            "ORDER BY p2.id LIMIT 1) "
            f"WHERE {where} ORDER BY speedbench_run_time(r.ts),r.id,n.id",
            params,
        ).fetchall()
        intel_by_run_ip = {}
        intel_rows = conn.execute(
            "SELECT run_id, exit_ip, ip_version, classification, confidence, "
            "ip_quality_score, ip_grade, ipqs_fraud_score, scamalytics_score, "
            "evidence_json, conflicts_json, provider_status_json, normalized_json "
            "FROM ip_intel_results ORDER BY run_id, rowid"
        ).fetchall()
        for row in intel_rows:
            intel_by_run_ip[(row[0], row[1])] = row
    finally:
        conn.close()

    timeline = []
    previous = None
    # The timeline interleaves IPv4/IPv6 observations for a dual-stack node.
    # Keep the adjacent output dedup state in ``previous``, but compare risk
    # changes against the most recent observation of the same exit IP.
    previous_by_ip = {}
    for row in rows:
        d = dict(row)
        run_id = d.pop("run_id")
        run_node_count = d.pop("run_node_count")
        d.pop("node_result_id", None)
        # New family columns are preferred, but old ip_profiles remains the
        # canonical fallback.  The list allows both IPv4 and IPv6 intel rows
        # to be attached to the same node without changing legacy tables.
        ips = []
        for value in (d.get("exit_ipv4"), d.get("exit_ipv6"), d.get("exit_ip")):
            value = _db_text(value)
            if value and value not in ips:
                ips.append(value)
        matches = [intel_by_run_ip[(run_id, ip)] for ip in ips
                   if (run_id, ip) in intel_by_run_ip]
        if not matches and not ips and run_node_count == 1:
            # A malformed/partial new row may have only an intelligence IP;
            # attach it only when the entire run has one node and one intel
            # row. One intel row alone does not identify a failed sibling.
            candidates = [row for (rid, _), row in intel_by_run_ip.items()
                          if rid == run_id]
            if len(candidates) == 1:
                matches = candidates
        if not matches:
            matches = [None]
        for intel in matches:
            item = {
                "ts": d.get("ts"),
                "name": d.get("name"),
                "node_key": d.get("node_key"),
                "exit_ip": d.get("exit_ip") or (intel[1] if intel else None),
                "exit_ipv4": d.get("exit_ipv4"),
                "exit_ipv6": d.get("exit_ipv6"),
                "country": d.get("country"),
                "country_code": d.get("country_code"),
                "isp": d.get("isp"),
                "org": d.get("org"),
                "asn": d.get("asn"),
                "asname": d.get("asname"),
                "kind": d.get("kind"),
                "proxy": bool(d["proxy"]) if d.get("proxy") is not None else None,
                "hosting": bool(d["hosting"]) if d.get("hosting") is not None else None,
                "mobile": bool(d["mobile"]) if d.get("mobile") is not None else None,
                "ip_version": None,
                "classification": None,
                "confidence": None,
                "ip_quality_score": None,
                "ip_grade": None,
                "ipqs_fraud_score": None,
                "scamalytics_score": None,
                "evidence": [],
                "conflicts": [],
                "provider_status": {},
                "normalized": {},
                "intel_available": False,
                "reputation_worsened": False,
            }
            if intel is not None:
                item.update({
                    "exit_ip": intel[1] or item["exit_ip"],
                    "ip_version": intel[2],
                    "classification": intel[3],
                    "confidence": intel[4],
                    "ip_quality_score": intel[5],
                    "ip_grade": intel[6],
                    "ipqs_fraud_score": intel[7],
                    "scamalytics_score": intel[8],
                    "evidence": _json_column(intel[9], []),
                    "conflicts": _json_column(intel[10], []),
                    "provider_status": _json_column(intel[11], {}),
                    "normalized": _json_column(intel[12], {}),
                    "intel_available": True,
                })
            # ``ip_quality_score``/grade are intentionally independent of the
            # old ``score`` field; no missing-intel row is awarded a clean IP.
            item_previous = previous_by_ip.get(item.get("exit_ip"))
            item["reputation_worsened"] = _reputation_worsened(item_previous, item)
            item["reputation_degraded"] = item["reputation_worsened"]
            item["same_ip_reputation_worsened"] = item["reputation_worsened"]
            identity = (
                item.get("exit_ip"), item.get("asn"), item.get("classification"),
                item.get("confidence"), item.get("ip_quality_score"),
                item.get("ip_grade"), item.get("ipqs_fraud_score"),
                item.get("scamalytics_score"), item.get("intel_available"),
            )
            previous_identity = previous and (
                previous.get("exit_ip"), previous.get("asn"), previous.get("classification"),
                previous.get("confidence"), previous.get("ip_quality_score"),
                previous.get("ip_grade"), previous.get("ipqs_fraud_score"),
                previous.get("scamalytics_score"), previous.get("intel_available"),
            )
            previous_by_ip[item.get("exit_ip")] = item
            if identity == previous_identity:
                continue
            timeline.append(item)
            previous = item
    return timeline


def insert_leak_audit(db_path, audit) -> int:
    """Persist one local WebRTC/DNS audit and return its SQLite id.

    This API accepts a mapping or a small dataclass exposing ``to_dict``.  It
    stores only serializable local observations; credential-shaped keys and
    configured secret values are removed before binding JSON parameters.
    """
    if hasattr(audit, "to_dict") and callable(audit.to_dict):
        audit = audit.to_dict()
    elif hasattr(audit, "as_dict") and callable(audit.as_dict):
        audit = audit.as_dict()
    if not isinstance(audit, dict):
        raise TypeError("audit must be a mapping or to_dict object")
    webrtc = audit.get("webrtc") if isinstance(audit.get("webrtc"), dict) else {}
    dns = audit.get("dns") if isinstance(audit.get("dns"), dict) else {}
    created = audit.get("created_at")
    if isinstance(created, (int, float)) and not isinstance(created, bool):
        created_at = int(created)
    else:
        try:
            created_at = int(float(created))
        except (TypeError, ValueError):
            created_at = int(time.time())
    candidates = audit.get("candidates", audit.get("ice_candidates", webrtc.get("candidates", [])))
    webrtc_status = audit.get("webrtc_status", webrtc.get("status", "unknown"))
    dns_mode = audit.get("dns_mode", dns.get("mode", "guided"))
    dns_status = audit.get("dns_status", dns.get("status"))
    exit_ipv4 = audit.get("exit_ipv4", audit.get("ipv4"))
    exit_ipv6 = audit.get("exit_ipv6", audit.get("ipv6"))
    reserved = {
        "created_at", "candidates", "ice_candidates", "webrtc", "webrtc_status",
        "dns", "dns_mode", "dns_status", "exit_ipv4", "exit_ipv6", "ipv4", "ipv6",
    }
    details = audit.get("details")
    if details is None:
        details = {str(k): v for k, v in audit.items() if k not in reserved}
    conn = _open(db_path)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO leak_audits("
                "created_at, exit_ipv4, exit_ipv6, webrtc_status, candidates_json, "
                "dns_mode, dns_status, details_json) VALUES (?,?,?,?,?,?,?,?)",
                (
                    created_at, _db_text(exit_ipv4), _db_text(exit_ipv6),
                    _db_text(webrtc_status, "unknown"), _safe_json(candidates, "[]"),
                    _db_text(dns_mode, "guided"), _db_text(dns_status),
                    _safe_json(details, "{}"),
                ),
            )
            return int(cur.lastrowid)
    finally:
        conn.close()


def leak_audits(db_path, limit: int = 20) -> list:
    """Return recent persisted local leak audits, newest first."""
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 20
    conn = _open(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, created_at, exit_ipv4, exit_ipv6, webrtc_status, "
            "candidates_json, dns_mode, dns_status, details_json "
            "FROM leak_audits ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        d = dict(row)
        d["candidates"] = _json_column(d.pop("candidates_json"), [])
        d["details"] = _json_column(d.pop("details_json"), {})
        out.append(d)
    return out


def _median_or_none(vals: list, ndigits: int):
    """非空数值列表的中位数（round 到 ndigits 位）；空列表返回 None。"""
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), ndigits) if vals else None


def source_summary(db_path, days=30, subscription_id=None):
    """Versioned origins, keeping legacy unknowns separate and honest coverage.

    Ambiguous membership is included in each possible source and explicitly
    counted. Consumers must not sum these source totals as unique global nodes.
    """
    days = max(1, min(int(days), 3650))
    since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S')
    conn = _open(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            'SELECT n.*,r.ts,o.subscription_id,o.name_snapshot,o.source_status AS origin_status,s.current_name '
            'FROM node_results n JOIN runs r ON r.id=n.run_id '
            'LEFT JOIN node_origins o ON o.node_result_id=n.id '
            'LEFT JOIN subscription_sources s ON s.subscription_id=o.subscription_id '
            'WHERE speedbench_run_time(r.ts)>=speedbench_run_time(?) ORDER BY speedbench_run_time(r.ts),r.id,n.id', (since,)).fetchall()
    finally:
        conn.close()
    groups = {}
    for row in rows:
        sid = row['subscription_id'] or ''
        if subscription_id is not None and sid != subscription_id:
            continue
        # Old provider labels are not verified subscriptions. They retain their
        # own bucket instead of being silently attached to today's source.
        legacy = (row['provider'] or '') if not sid else ''
        key = (sid, legacy, bool(row['node_id']))
        group = groups.setdefault(key, dict(subscription_id=sid,
            name=row['current_name'] if sid else legacy or ('来源未知' if row['node_id'] else '历史来源未知'),
            source_status='verified' if sid else 'unknown' if row['node_id'] else 'legacy_unknown',
            rows=[], runs=set(), nodes=set(), ambiguous_node_count=0, last_ts=''))
        group['rows'].append(row)
        group['runs'].add(row['run_id'])
        group['nodes'].add(row['node_id'] or ('legacy', row['name']))
        if row['origin_status'] == 'ambiguous':
            group['ambiguous_node_count'] += 1
            group['source_status'] = 'ambiguous'
        group['last_ts'] = max(group['last_ts'], row['ts'], key=_time_key)
    result = []
    for group in groups.values():
        rows = group.pop('rows')
        count = len(rows)
        # Missing old probe data is neither success nor failure. Report known
        # probe coverage separately, never silently classify unknown as offline.
        probed = [r for r in rows if (r['probe_attempts'] and r['probe_successes'] is not None)
                  or r['latency_ms'] is not None]
        online = sum((r['probe_successes'] > 0 if r['probe_attempts'] and r['probe_successes'] is not None
                      else True) for r in probed)
        attempted = sum(r['sample_mb'] is not None or r['median_mbps'] is not None for r in rows)
        successful = sum(r['median_mbps'] is not None and r['median_mbps'] > 0 for r in rows)
        scores = [r['network_score'] for r in rows if r['network_score'] is not None]
        run_count, node_count = len(group.pop('runs')), len(group.pop('nodes'))
        result.append(dict(group, run_count=run_count, node_count=node_count,
            probe_online_ratio=round(online/len(probed),4) if probed else None,
            probe_coverage=round(len(probed)/count,4), bandwidth_coverage=round(attempted/count,4),
            bandwidth_success_ratio=round(successful/attempted,4) if attempted else None,
            median_mbps=_median_or_none([r['median_mbps'] for r in rows],3),
            latency_ms=_median_or_none([r['latency_ms'] for r in rows],1),
            avg_network_score=round(statistics.fmean(scores),1) if scores else None))
    result.sort(key=lambda item: (_time_key(item['last_ts']),item['name']), reverse=True)
    return result


def source_series(db_path, subscription_id, days=30):
    """Per-run source-ID coverage; old provider names are never evidence."""
    from contextlib import closing
    since=(datetime.now()-timedelta(days=max(1,min(int(days),3650)))).isoformat(timespec='seconds')
    with closing(_open(db_path)) as conn:
        conn.row_factory=sqlite3.Row
        rows=conn.execute('''SELECT n.*,r.ts,o.name_snapshot FROM node_results n
            JOIN runs r ON r.id=n.run_id JOIN node_origins o ON o.node_result_id=n.id
            WHERE o.subscription_id=? AND speedbench_run_time(r.ts)>=speedbench_run_time(?) ORDER BY speedbench_run_time(r.ts),r.id,n.id''',
            (subscription_id,since)).fetchall()
    grouped={}
    for row in rows:
        grouped.setdefault(row['run_id'],[]).append(row)
    result=[]
    for rows in grouped.values():
        probed=[r for r in rows if r['probe_attempts'] or r['latency_ms'] is not None]
        online=sum(r['probe_successes']>0 if r['probe_attempts'] and r['probe_successes'] is not None
                   else r['latency_ms'] is not None for r in probed)
        attempted=sum(r['sample_mb'] is not None or r['median_mbps'] is not None for r in rows)
        successful=sum(r['median_mbps'] is not None and r['median_mbps']>0 for r in rows)
        scores=[r['network_score'] for r in rows if r['network_score'] is not None]
        result.append(dict(ts=rows[0]['ts'],name_snapshot=rows[0]['name_snapshot'],node_count=len(rows),
            online_ratio=online/len(probed) if probed else None,probe_coverage=len(probed)/len(rows),
            bandwidth_coverage=attempted/len(rows),bandwidth_success_ratio=successful/attempted if attempted else None,
            median_mbps=_median_or_none([r['median_mbps'] for r in rows],3),
            latency_ms=_median_or_none([r['latency_ms'] for r in rows],1),
            avg_score=statistics.fmean(scores) if scores else None))
    return result


def subscription_summary(db_path, days: int = 30) -> list:
    """按订阅（provider）聚合最近 days 天，供 /api/subscriptions。

    每项：provider / run_count（出现过的轮次数）/ node_count（去重节点数）/
    online_ratio（status='ok' 占比）/ median_mbps / latency_ms / avg_score / last_ts。
    provider 缺失/为空统一归 ""（Web 层再转成「(未知订阅)」展示）；
    中位数在 Python 侧用 statistics 算（单订阅几十到几百行，无需 SQL 窗口函数）。
    """
    days = max(1, min(int(days), 3650))
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _open(db_path)
    try:
        rows = conn.execute(
            "SELECT COALESCE(n.provider, ''), n.run_id, r.ts, n.name,"
            " n.status, n.median_mbps, n.latency_ms, n.score"
            " FROM node_results n JOIN runs r ON r.id = n.run_id"
            " WHERE speedbench_run_time(r.ts) >= speedbench_run_time(?)"
            " ORDER BY speedbench_run_time(r.ts),r.id,n.id",
            (since,)).fetchall()
    finally:
        conn.close()
    groups: dict = {}
    for provider, run_id, ts, name, status, med, lat, score in rows:
        g = groups.setdefault(provider, {
            "run_ids": set(), "names": set(), "total": 0, "ok": 0,
            "meds": [], "lats": [], "scores": [], "last_ts": "",
        })
        g["run_ids"].add(run_id)
        g["names"].add(name)
        g["total"] += 1
        if status == "ok":
            g["ok"] += 1
        g["meds"].append(med)
        g["lats"].append(lat)
        g["scores"].append(score)
        if _time_key(ts) > _time_key(g["last_ts"]):
            g["last_ts"] = ts
    out = []
    for provider, g in groups.items():
        scores = [s for s in g["scores"] if s is not None]
        out.append({
            "provider": provider,
            "run_count": len(g["run_ids"]),
            "node_count": len(g["names"]),
            "online_ratio": round(g["ok"] / g["total"], 4) if g["total"] else 0.0,
            "median_mbps": _median_or_none(g["meds"], 3),
            "latency_ms": _median_or_none(g["lats"], 1),
            "avg_score": round(statistics.fmean(scores), 1) if scores else None,
            "last_ts": g["last_ts"],
        })
    out.sort(key=lambda d: (_time_key(d["last_ts"]), d["provider"]), reverse=True)
    return out


def subscription_series(db_path, provider: str, days: int = 30) -> list:
    """单订阅按轮次的时间序列，供 /api/subscription。

    每轮对该订阅全部节点聚合：online_ratio / median_mbps / latency_ms /
    avg_score；时间升序。provider="" 匹配无订阅来源的历史行。
    """
    days = max(1, min(int(days), 3650))
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _open(db_path)
    try:
        rows = conn.execute(
            "SELECT r.id, r.ts, n.status, n.median_mbps, n.latency_ms, n.score"
            " FROM node_results n JOIN runs r ON r.id = n.run_id"
            " WHERE COALESCE(n.provider, '') = ? AND speedbench_run_time(r.ts) >= speedbench_run_time(?)"
            " ORDER BY speedbench_run_time(r.ts),r.id,n.id",
            (provider or "", since)).fetchall()
    finally:
        conn.close()
    per_run: dict = {}
    order: list = []
    for run_id, ts, status, med, lat, score in rows:
        if run_id not in per_run:
            per_run[run_id] = {"ts": ts, "total": 0, "ok": 0,
                               "meds": [], "lats": [], "scores": []}
            order.append(run_id)
        g = per_run[run_id]
        g["total"] += 1
        if status == "ok":
            g["ok"] += 1
        g["meds"].append(med)
        g["lats"].append(lat)
        g["scores"].append(score)
    out = []
    for run_id in order:
        g = per_run[run_id]
        scores = [s for s in g["scores"] if s is not None]
        out.append({
            "ts": g["ts"],
            "online_ratio": round(g["ok"] / g["total"], 4) if g["total"] else 0.0,
            "median_mbps": _median_or_none(g["meds"], 3),
            "latency_ms": _median_or_none(g["lats"], 1),
            "avg_score": round(statistics.fmean(scores), 1) if scores else None,
        })
    return out
