# -*- coding: utf-8 -*-
"""订阅维度（v0.9）单元测试。

- classify_failure：status → 失败分类的全部分支
- node_key_of：server/port 路径稳定（改名不断链）、退化路径、凭据变化换 key
- result_to_dict：新增 node_key / fail_reason 字段
- 旧格式 jsonl（无 node_key/fail_reason/provider）导入 + 回放：不崩、默认值空
- DB 迁移：手工建旧 schema → 打开触发 _ensure_columns → 新列存在、旧数据保留、
  新行可写可查；重复打开幂等
- subscription_summary / subscription_series：两订阅 × 多轮 × 部分失败的聚合正确性
- /api/subscriptions、/api/subscription、/api/node?key= 的 HTTP 级行为

全部在 TemporaryDirectory 里造 jsonl/db，不碰仓库根目录的真实历史文件。
"""
import json
import sqlite3
import sys
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clash_speedbench as csb
import speedbench_db as db
import speedbench_web as web
from tests.web_server_case import WebServerCase

_TS_FMT = "%Y-%m-%dT%H:%M:%S"
_BASE = datetime.now()


def ts_before(**kw):
    return (_BASE - timedelta(**kw)).strftime(_TS_FMT)


def make_result(name, provider="机场甲", median=100.0, latency=80, score=80.0,
                status=None, node_key="", fail_reason=None):
    """单节点结果；median=None 默认视为失败轮（status 可显式覆盖）。"""
    if status is None:
        status = "ok" if median is not None else "curl-timeout"
    r = {"name": name, "proto": "ss", "provider": provider,
         "latency_ms": latency, "median_mbps": median,
         "best_mbps": None if median is None else median + 5,
         "score": score, "stars": "★★★★☆", "status": status,
         "tags": "", "node_key": node_key}
    if fail_reason is not None:
        r["fail_reason"] = fail_reason
    return r


def make_record(ts, results, mb=30, rounds=1):
    return {"ts": ts, "mb": mb, "rounds": rounds, "results": results}


class ClassifyFailureTest(unittest.TestCase):
    def test_ok_and_empty_map_to_empty(self):
        self.assertEqual(csb.classify_failure("ok"), "")
        self.assertEqual(csb.classify_failure(""), "")
        self.assertEqual(csb.classify_failure(None), "")

    def test_known_categories(self):
        cases = {
            "curl-timeout": "timeout",
            "curl-timeout;curl-timeout": "timeout",   # 多轮拼接
            "no-data": "no_data",
            "no-data: connection closed": "no_data",
            "http-403": "http_error",
            "http-522": "http_error",
            "curl-7: Failed to connect": "connect_error",
            "unreachable": "connect_error",           # workers 延迟探测不通
            "switch-failed": "switch_failed",
            "switch-failed: boom": "switch_failed",   # workers 精测切换失败
            "parse-error": "other",
            "error: something": "other",
            "worker-failed: something": "other",
            "mystery": "other",                       # 未识别非 ok 一律 other
        }
        for status, want in cases.items():
            with self.subTest(status=status):
                self.assertEqual(csb.classify_failure(status), want)


class NodeKeyOfTest(unittest.TestCase):
    def test_same_credentials_different_name_same_key(self):
        k1 = csb.node_key_of("ss", "a.example.com", "8388", "节点A")
        k2 = csb.node_key_of("ss", "a.example.com", "8388", "改名后的节点A")
        self.assertEqual(k1, k2)          # 改名不断链
        self.assertEqual(len(k1), 12)

    def test_credentials_change_changes_key(self):
        base = csb.node_key_of("ss", "a.example.com", "8388", "节点A")
        self.assertNotEqual(csb.node_key_of("ss", "b.example.com", "8388", "节点A"), base)
        self.assertNotEqual(csb.node_key_of("ss", "a.example.com", "443", "节点A"), base)
        self.assertNotEqual(csb.node_key_of("trojan", "a.example.com", "8388", "节点A"), base)

    def test_fallback_uses_proto_and_name(self):
        k1 = csb.node_key_of("ss", "", "", "节点A")
        self.assertEqual(k1, csb.node_key_of("ss", None, None, "节点A"))  # 退化路径稳定
        self.assertNotEqual(k1, csb.node_key_of("ss", "", "", "节点B"))   # 改名即断链
        # 退化路径与完整路径不串：同名同 proto 但一个有凭据一个没有，key 不同
        self.assertNotEqual(k1, csb.node_key_of("ss", "a.example.com", "8388", "节点A"))


class ResultToDictNewFieldsTest(unittest.TestCase):
    def _result(self, status="ok"):
        return csb.Result(name="节点X", provider="机场甲", proto="ss",
                          latency_ms=80, speeds_mbps=[100.0], median_mbps=100.0,
                          best_mbps=100.0, status=status,
                          node_key=csb.node_key_of("ss", "a.example.com", "8388", "节点X"))

    def test_node_key_and_fail_reason_present(self):
        d = csb.result_to_dict(self._result())
        self.assertEqual(d["node_key"], self._result().node_key)
        self.assertEqual(d["fail_reason"], "")          # ok → 空串

    def test_fail_reason_derived_from_status(self):
        d = csb.result_to_dict(self._result(status="curl-timeout"))
        self.assertEqual(d["fail_reason"], "timeout")

    def test_node_key_defaults_to_empty(self):
        r = self._result()
        r.node_key = ""
        self.assertEqual(csb.result_to_dict(r)["node_key"], "")


class OldFormatCompatTest(unittest.TestCase):
    """旧格式 jsonl 行（无 node_key/fail_reason/provider）导入与回放不报错。"""

    def test_old_row_defaults_empty(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            jsonl = td / "h.jsonl"
            dbp = td / "h.db"
            old = {"ts": ts_before(), "mb": 20, "rounds": 1,
                   "results": [{"name": "旧节点", "proto": "ss",
                                "latency_ms": 120, "median_mbps": 45.5,
                                "score": 66.6, "status": "ok"}]}
            jsonl.write_text(json.dumps(old, ensure_ascii=False) + "\n",
                             encoding="utf-8")
            self.assertEqual(db.import_jsonl(dbp, jsonl), 1)
            row = db.all_runs(dbp)[0]["results"][0]
            self.assertNotIn("node_key", row)           # raw 回放逐字段保真
            conn = sqlite3.connect(str(dbp))
            try:
                r = conn.execute(
                    "SELECT provider, node_key, fail_reason FROM node_results"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(r, ("", "", ""))           # 索引表落默认空串
            # 旧行归入未知订阅，聚合不崩
            summary = db.subscription_summary(dbp, days=30)
            self.assertEqual(len(summary), 1)
            self.assertEqual(summary[0]["provider"], "")


# 旧 schema（v0.8 及更早）：node_results 无 node_key/fail_reason，
# ip_profiles 无 region/city，且无 provider 索引
OLD_SCHEMA = """
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL UNIQUE,
    mb REAL, rounds INTEGER, node_count INTEGER NOT NULL DEFAULT 0,
    raw TEXT NOT NULL);
CREATE TABLE node_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    name TEXT NOT NULL, proto TEXT, provider TEXT,
    latency_ms REAL, jitter_ms REAL, connect_ms REAL,
    median_mbps REAL, best_mbps REAL, multi_mbps REAL, sample_mb REAL,
    score REAL, stars TEXT, status TEXT, tags TEXT);
CREATE INDEX idx_node_results_name ON node_results(name);
CREATE TABLE ip_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    name TEXT NOT NULL, exit_ip TEXT, country TEXT, country_code TEXT,
    isp TEXT, org TEXT, asn TEXT, asname TEXT, kind TEXT,
    ok INTEGER, proxy INTEGER, hosting INTEGER, mobile INTEGER);
CREATE INDEX idx_ip_profiles_name ON ip_profiles(name);
"""


class DbMigrationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        td = Path(self._tmp.name)
        self.db_path = td / "h.db"
        self.jsonl = td / "h.jsonl"
        # 手工建旧 schema 库，并塞一行旧数据
        conn = sqlite3.connect(str(self.db_path))
        with conn:
            conn.executescript(OLD_SCHEMA)
            cur = conn.execute(
                "INSERT INTO runs(ts, mb, rounds, node_count, raw)"
                " VALUES ('2026-08-20T10:00:00', 20, 1, 1, '{}')")
            conn.execute(
                "INSERT INTO node_results(run_id, name, proto, provider, status)"
                " VALUES (?, '旧节点', 'ss', '机场甲', 'ok')", (cur.lastrowid,))
        conn.close()

    def columns(self, table):
        conn = sqlite3.connect(str(self.db_path))
        try:
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        finally:
            conn.close()

    def test_ensure_columns_upgrades_old_db(self):
        db.import_jsonl(self.db_path, self.jsonl)   # 打开即触发 _ensure_columns
        self.assertIn("node_key", self.columns("node_results"))
        self.assertIn("fail_reason", self.columns("node_results"))
        self.assertIn("region", self.columns("ip_profiles"))
        self.assertIn("city", self.columns("ip_profiles"))
        # 旧数据保留
        conn = sqlite3.connect(str(self.db_path))
        try:
            row = conn.execute(
                "SELECT name, provider, node_key, fail_reason FROM node_results"
            ).fetchone()
            idx = [r[1] for r in conn.execute(
                "PRAGMA index_list(node_results)").fetchall()]
        finally:
            conn.close()
        self.assertEqual(row, ("旧节点", "机场甲", None, None))  # 旧行新列为 NULL
        self.assertIn("idx_node_results_provider", idx)

    def test_new_rows_writable_after_migration(self):
        rec = make_record(ts_before(), [
            make_result("节点A", provider="机场乙", median=88.8,
                        node_key="k" * 12, fail_reason="")])
        self.jsonl.write_text(json.dumps(rec, ensure_ascii=False) + "\n",
                              encoding="utf-8")
        self.assertEqual(db.import_jsonl(self.db_path, self.jsonl), 1)
        rows = db.subscription_summary(self.db_path, days=30)
        by_provider = {r["provider"]: r for r in rows}
        self.assertEqual(by_provider["机场乙"]["median_mbps"], 88.8)
        self.assertEqual(by_provider["机场甲"]["run_count"], 1)  # 旧行参与聚合

    def test_ensure_columns_idempotent(self):
        db.import_jsonl(self.db_path, self.jsonl)
        db.import_jsonl(self.db_path, self.jsonl)     # 重复打开不重复加列
        db.all_runs(self.db_path)                      # 再走一次 _open 也不崩
        self.assertIn("node_key", self.columns("node_results"))


class SubscriptionAggregateTest(unittest.TestCase):
    """两订阅 × 多轮 × 部分失败：核对 online_ratio / 中位数 / 轮次数 / 天数窗口。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        td = Path(self._tmp.name)
        self.db_path = td / "h.db"
        self.jsonl = td / "h.jsonl"
        recs = [
            # 40 天前的一轮：应被默认 30 天窗口滤掉
            make_record(ts_before(days=40), [
                make_result("节点A", median=999.0, score=99.0)]),
            make_record(ts_before(hours=3), [
                make_result("节点A", median=100.0, latency=60, score=80.0),
                make_result("节点B", median=None, latency=None, score=0.0),
                make_result("节点C", provider="机场乙", median=40.0, latency=120,
                            score=70.0),
                make_result("节点D", provider="", median=10.0, latency=200,
                            score=10.0)]),
            make_record(ts_before(hours=1), [
                make_result("节点A", median=200.0, latency=80, score=90.0),
                make_result("节点B", median=50.0, latency=100, score=60.0)]),
        ]
        self.jsonl.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs),
            encoding="utf-8")
        db.import_jsonl(self.db_path, self.jsonl)

    def test_summary_per_provider(self):
        rows = db.subscription_summary(self.db_path, days=30)
        by = {r["provider"]: r for r in rows}
        self.assertEqual(set(by), {"机场甲", "机场乙", ""})
        ja = by["机场甲"]
        self.assertEqual(ja["run_count"], 2)            # 40 天前那轮被滤掉
        self.assertEqual(ja["node_count"], 2)
        self.assertAlmostEqual(ja["online_ratio"], 0.75)        # 4 行 3 行 ok
        self.assertAlmostEqual(ja["median_mbps"], 100.0)        # median(100,200,50)
        self.assertAlmostEqual(ja["latency_ms"], 80.0)          # median(60,80,100)
        self.assertAlmostEqual(ja["avg_score"], 57.5)           # (80+0+90+60)/4
        jb = by["机场乙"]
        self.assertEqual((jb["run_count"], jb["node_count"]), (1, 1))
        self.assertAlmostEqual(jb["online_ratio"], 1.0)
        unk = by[""]
        self.assertEqual(unk["node_count"], 1)                  # 无订阅来源的旧行
        # 排序：最近测速新在前
        self.assertGreaterEqual(rows[0]["last_ts"], rows[-1]["last_ts"])

    def test_summary_days_window(self):
        rows60 = {r["provider"]: r for r in db.subscription_summary(self.db_path, days=60)}
        self.assertEqual(rows60["机场甲"]["run_count"], 3)        # 60 天窗口含旧轮
        self.assertAlmostEqual(rows60["机场甲"]["median_mbps"], 150.0)  # median(999,100,200,50)

    def test_series_per_run(self):
        series = db.subscription_series(self.db_path, "机场甲", days=30)
        self.assertEqual(len(series), 2)                # 两轮，时间升序
        r1, r2 = series
        self.assertLess(r1["ts"], r2["ts"])
        self.assertAlmostEqual(r1["online_ratio"], 0.5)         # 2 节点 1 通
        self.assertAlmostEqual(r1["median_mbps"], 100.0)        # 失败节点 median 不计
        self.assertAlmostEqual(r1["avg_score"], 40.0)           # (80+0)/2
        self.assertAlmostEqual(r2["online_ratio"], 1.0)
        self.assertAlmostEqual(r2["median_mbps"], 125.0)        # median(200,50)
        self.assertAlmostEqual(r2["avg_score"], 75.0)           # (90+60)/2

    def test_series_unknown_provider_and_empty(self):
        rows = db.subscription_series(self.db_path, "", days=30)
        self.assertEqual(len(rows), 1)                  # provider='' 的旧行可查
        self.assertEqual(db.subscription_series(self.db_path, "不存在订阅"), [])


class SubscriptionApiTest(WebServerCase):
    """HTTP 级：/api/subscriptions、/api/subscription、/api/node?key=。"""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hist = Path(self._tmp.name) / "speedbench-history.jsonl"

    def get_json(self, path):
        from unittest import mock
        with mock.patch.object(web, "HISTORY", self.hist):
            status, body = self.request("GET", path)
        return status, json.loads(body.decode("utf-8"))

    def _seed(self):
        recs = [
            make_record(ts_before(hours=3), [
                make_result("节点A", median=100.0, latency=60, score=80.0,
                            node_key="a" * 12),
                make_result("节点B", median=None, latency=None, score=0.0),
                make_result("节点D", provider="", median=10.0, score=10.0)]),
            make_record(ts_before(hours=1), [
                make_result("节点A·改名", median=200.0, latency=80, score=90.0,
                            node_key="a" * 12),       # 同 node_key，换了名字
                make_result("节点B", median=50.0, latency=100, score=60.0)]),
        ]
        self.hist.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs),
            encoding="utf-8")

    def test_subscriptions_summary(self):
        self._seed()
        status, rows = self.get_json("/api/subscriptions")
        self.assertEqual(status, 200)
        names = {r["provider"] for r in rows}
        self.assertEqual(names, {"机场甲", "(未知订阅)"})  # 空 provider 转成展示名
        ja = next(r for r in rows if r["provider"] == "机场甲")
        self.assertEqual(ja["run_count"], 2)
        self.assertEqual(ja["node_count"], 3)   # 按节点名去重：改名后的 A 另算一个
        self.assertAlmostEqual(ja["online_ratio"], 0.75)

    def test_subscriptions_empty_db_returns_empty_list(self):
        status, rows = self.get_json("/api/subscriptions")
        self.assertEqual(status, 200)
        self.assertEqual(rows, [])

    def test_subscription_series_and_unknown_name(self):
        self._seed()
        path = "/api/subscription?" + urllib.parse.urlencode(
            {"name": "机场甲", "days": 30})
        status, series = self.get_json(path)
        self.assertEqual(status, 200)
        self.assertEqual(len(series), 2)
        self.assertAlmostEqual(series[0]["online_ratio"], 0.5)
        self.assertAlmostEqual(series[1]["median_mbps"], 125.0)
        # "(未知订阅)" 与空串都查 provider='' 的行
        for name in ("(未知订阅)", ""):
            _, s2 = self.get_json("/api/subscription?"
                                  + urllib.parse.urlencode({"name": name}))
            self.assertEqual(len(s2), 1)
            self.assertAlmostEqual(s2[0]["median_mbps"], 10.0)

    def test_node_by_key_covers_rename(self):
        self._seed()
        status, d = self.get_json("/api/node?key=" + "a" * 12 + "&days=30")
        self.assertEqual(status, 200)
        self.assertEqual([s["median_mbps"] for s in d["series"]], [100.0, 200.0])
        self.assertEqual(d["ip_changes"], [])           # key 查询无 name：时间线为空
        # 旧行为不回归：按 name 仍只看到自己
        _, d2 = self.get_json("/api/node?name="
                              + urllib.parse.quote("节点A") + "&days=30")
        self.assertEqual([s["median_mbps"] for s in d2["series"]], [100.0])
        # key 与 name 都缺仍是 400
        status, d3 = self.get_json("/api/node?days=30")
        self.assertEqual(status, 400)
        self.assertFalse(d3["ok"])


if __name__ == "__main__":
    unittest.main()
