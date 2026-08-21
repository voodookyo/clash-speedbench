#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Clash SpeedBench 历史库（SQLite，零第三方依赖）。

speedbench-history.jsonl 仍是原始备份（只追加、不删除）；本模块把它镜像成
同目录的 speedbench-history.db，供 Web 面板走 SQL 查询：

- import_jsonl()  增量导入：按 runs.ts 去重，幂等，坏行跳过；
- latest_run()    最后一轮记录（结构与 jsonl 行完全一致，/api/latest 原样返回）；
- all_runs()      全部轮次（同结构列表，/api/history 用）；
- node_series()   某节点最近 N 天逐次测速的 带宽/延迟/抖动 序列；
- ip_changes()    某节点出口 IP / ASN 变化时间线。

结构保真的做法：runs.raw 直接存原始 jsonl 行文本，读取端 json.loads 回放，
因此旧行缺新字段、老 ip 结构（risk、kind="住宅" 等）都能原样通过，前端零改动；
node_results / ip_profiles 只是为查询建的索引表，缺失字段一律存 NULL。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
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
    tags        TEXT
);
CREATE INDEX IF NOT EXISTS idx_node_results_name ON node_results(name);
CREATE TABLE IF NOT EXISTS ip_profiles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(id),
    name         TEXT NOT NULL,
    exit_ip      TEXT,
    country      TEXT,
    country_code TEXT,
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
"""


def _open(db_path) -> sqlite3.Connection:
    """打开（必要时创建）历史库并确保表结构存在。WAL：读查询不阻塞导入。"""
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def _bool_or_none(v):
    """布尔/缺失 → 1/0/NULL（旧格式没有 proxy/hosting/mobile 时保持 NULL）。"""
    return None if v is None else int(bool(v))


def _insert_result(conn: sqlite3.Connection, run_id: int, r: dict) -> None:
    name = str(r.get("name") or "")
    conn.execute(
        "INSERT INTO node_results(run_id, name, proto, provider, latency_ms,"
        " jitter_ms, connect_ms, median_mbps, best_mbps, multi_mbps, sample_mb,"
        " score, stars, status, tags) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, name, r.get("proto"), r.get("provider"),
         r.get("latency_ms"), r.get("jitter_ms"), r.get("connect_ms"),
         r.get("median_mbps"), r.get("best_mbps"), r.get("multi_mbps"),
         r.get("sample_mb"), r.get("score"), r.get("stars"),
         r.get("status"), r.get("tags")))
    ip = r.get("ip")
    if not isinstance(ip, dict) or not ip:
        return
    # 旧格式（v0.2 及更早）的 ip 没有 ok 字段：按 exit_ip 是否存在推断查询成功
    ok = ip.get("ok")
    if ok is None:
        ok = bool(ip.get("exit_ip"))
    conn.execute(
        "INSERT INTO ip_profiles(run_id, name, exit_ip, country, country_code,"
        " isp, org, asn, asname, kind, ok, proxy, hosting, mobile)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, name, ip.get("exit_ip"), ip.get("country"),
         ip.get("country_code"), ip.get("isp"), ip.get("org"), ip.get("asn"),
         ip.get("asname"), ip.get("kind"), int(bool(ok)),
         _bool_or_none(ip.get("proxy")), _bool_or_none(ip.get("hosting")),
         _bool_or_none(ip.get("mobile"))))


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
                for line in f:
                    line = line.strip()
                    if not line:
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
            "SELECT raw FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else {}
    finally:
        conn.close()


def all_runs(db_path) -> list:
    """全部测速轮次（按写入先后升序，每项与 jsonl 行结构完全一致）。"""
    conn = _open(db_path)
    try:
        return [json.loads(row[0])
                for row in conn.execute("SELECT raw FROM runs ORDER BY id")]
    finally:
        conn.close()


def node_series(db_path, name: str, days: int = 30) -> list:
    """某节点最近 days 天逐次测速序列（时间升序）。

    ts 是 ISO 本地时间字符串，字典序即时间序，直接与 cutoff 比较。
    """
    days = max(1, min(int(days), 3650))
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _open(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT r.ts, n.median_mbps, n.best_mbps, n.multi_mbps,"
            " n.latency_ms, n.jitter_ms, n.connect_ms, n.score, n.status"
            " FROM node_results n JOIN runs r ON r.id = n.run_id"
            " WHERE n.name = ? AND r.ts >= ?"
            " ORDER BY r.id, n.id",
            (name, since)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def ip_changes(db_path, name: str) -> list:
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
            " ORDER BY r.id, p.id",
            (name,)).fetchall()
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
