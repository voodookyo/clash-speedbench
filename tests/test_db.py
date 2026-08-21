# -*- coding: utf-8 -*-
"""speedbench_db（v0.5 SQLite 历史库）单元测试。

jsonl 仍是原始备份，DB 是它的可查询镜像；本文件覆盖：
- import_jsonl：新库导入计数、二次导入幂等（返回 0 且三表行数不变）、
  坏行跳过、文件内重复 ts 只入一次、旧格式行（risk / kind=住宅 / 缺新字段）
  不崩、增量导入、文件缺失
- latest_run / all_runs：与 jsonl 行逐字段一致（raw 回放）、顺序与数量
- node_series：days 窗口过滤、字段齐全、参数钳制、未知节点为空
- ip_changes：相邻去重的 exit_ip/asn 变化点、失败轮次（无 exit_ip）不参与、
  布尔标记还原、旧格式标记保持 NULL

全部在 TemporaryDirectory 里造 jsonl/db，不碰仓库根目录的真实历史文件。
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import speedbench_db as db

_TS_FMT = "%Y-%m-%dT%H:%M:%S"
_BASE = datetime.now()


def ts_before(**kw):
    """当前时刻往前推 kw（如 hours=2 / days=40）的 ISO 本地时间串。"""
    return (_BASE - timedelta(**kw)).strftime(_TS_FMT)


def make_result(name, median=50.0, latency=100, jitter=5.0, score=60.0,
                exit_ip="203.0.113.1", asn="AS64496 Example Net"):
    """新格式（v0.5）单节点结果；exit_ip=None 表示 IP 查询失败。"""
    r = {
        "name": name, "proto": "ss", "provider": "机场甲",
        "latency_ms": latency, "jitter_ms": jitter, "connect_ms": 88.5,
        "median_mbps": median,
        "best_mbps": None if median is None else median + 2,
        "multi_mbps": None if median is None else median * 2,
        "sample_mb": 30, "score": score, "stars": "★★★☆☆",
        "status": "ok" if median is not None else "timeout",
        "tags": "低延迟" if median is not None else "不通",
    }
    if exit_ip:
        r["ip"] = {"exit_ip": exit_ip, "country": "新加坡", "country_code": "SG",
                   "isp": "Example ISP", "org": "Example Org", "asn": asn,
                   "asname": "EXAMPLE-NET", "kind": "机房托管",
                   "proxy": False, "hosting": True, "mobile": False, "ok": True}
    else:
        r["ip"] = {"ok": False}
    return r


def make_record(ts, results, mb=30, rounds=1):
    return {"ts": ts, "mb": mb, "rounds": rounds, "results": results}


# 旧格式（v0.2 及更早）：有 risk、ip.kind 为「住宅」，缺 jitter_ms/connect_ms/
# multi_mbps/sample_mb，ip 里没有 ok/country_code/asname/布尔标记。
OLD_RECORD = {
    "ts": "2026-08-20T10:00:00",
    "mb": 20,
    "rounds": 2,
    "csv": "clash-speedtest-20260820.csv",
    "results": [
        {
            "name": "旧节点A", "provider": "机场甲", "proto": "ss",
            "latency_ms": 120, "median_mbps": 45.5, "best_mbps": 50.1,
            "score": 66.6, "stars": "★★★☆☆", "tags": "低延迟,住宅",
            "status": "ok", "risk": "低",
            "ip": {"exit_ip": "203.0.113.9", "country": "美国",
                   "asn": "AS13335 Cloudflare, Inc.", "isp": "Cloudflare",
                   "kind": "住宅", "risk": "低"},
        },
        {
            "name": "旧节点B", "provider": "机场甲", "proto": "trojan",
            "latency_ms": None, "median_mbps": None, "best_mbps": None,
            "score": 0, "stars": "☆☆☆☆☆", "tags": "不通",
            "status": "timeout", "risk": "高", "ip": {},
        },
    ],
}


class DbCase(unittest.TestCase):
    """每个用例一个独立临时目录，jsonl/db 都在里面。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        td = Path(self._tmp.name)
        self.db_path = td / "speedbench-history.db"
        self.jsonl = td / "speedbench-history.jsonl"

    def write_jsonl_lines(self, lines):
        self.jsonl.write_text("".join(l + "\n" for l in lines), encoding="utf-8")

    def dump_records(self, records):
        self.write_jsonl_lines([json.dumps(r, ensure_ascii=False) for r in records])

    def table_counts(self):
        conn = sqlite3.connect(str(self.db_path))
        try:
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("runs", "node_results", "ip_profiles")}
        finally:
            conn.close()

    def query(self, sql, params=()):
        conn = sqlite3.connect(str(self.db_path))
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


class ImportJsonlTest(DbCase):
    def _sample_records(self):
        return [
            make_record(ts_before(hours=3),
                        [make_result("节点A"), make_result("节点B", median=None, exit_ip=None)]),
            make_record(ts_before(hours=2), [make_result("节点A")]),
            make_record(ts_before(hours=1), [make_result("节点C", median=None, exit_ip=None)]),
        ]

    def test_fresh_import_returns_count(self):
        self.dump_records(self._sample_records())
        self.assertEqual(db.import_jsonl(self.db_path, self.jsonl), 3)
        # ip={"ok": false} 的非空 dict 也会出画像行（exit_ip 存 NULL）；ip={} 才没有
        self.assertEqual(self.table_counts(),
                         {"runs": 3, "node_results": 4, "ip_profiles": 4})

    def test_second_import_is_idempotent(self):
        self.dump_records(self._sample_records())
        self.assertEqual(db.import_jsonl(self.db_path, self.jsonl), 3)
        before = self.table_counts()
        self.assertEqual(db.import_jsonl(self.db_path, self.jsonl), 0)
        self.assertEqual(self.table_counts(), before)  # 三表行数不变

    def test_bad_lines_skipped(self):
        good1 = make_record(ts_before(hours=2), [make_result("节点A")])
        good2 = make_record(ts_before(hours=1), [make_result("节点A")])
        self.write_jsonl_lines([
            json.dumps(good1, ensure_ascii=False),
            "{这不是合法JSON",
            "[1, 2, 3]",                              # 合法 JSON 但不是对象
            '"只是一串字符串"',
            json.dumps({"mb": 5, "results": []}),     # 缺 ts
            json.dumps({"ts": "", "results": []}),    # ts 空串
            "",                                       # 空行
            json.dumps(good2, ensure_ascii=False),
        ])
        self.assertEqual(db.import_jsonl(self.db_path, self.jsonl), 2)
        self.assertEqual(self.table_counts()["runs"], 2)
        self.assertEqual([r["ts"] for r in db.all_runs(self.db_path)],
                         [good1["ts"], good2["ts"]])

    def test_duplicate_ts_in_file_imported_once(self):
        rec1 = make_record("2026-08-21T10:00:00", [make_result("节点A")], mb=10)
        rec2 = make_record("2026-08-21T10:00:00", [make_result("节点B")], mb=99)
        self.dump_records([rec1, rec2])
        self.assertEqual(db.import_jsonl(self.db_path, self.jsonl), 1)
        self.assertEqual(db.latest_run(self.db_path)["mb"], 10)  # 同一 ts 先到先得

    def test_record_without_results_imports_as_empty_run(self):
        self.dump_records([{"ts": ts_before(), "mb": 5},
                           {"ts": ts_before(hours=1), "results": "不是列表"}])
        self.assertEqual(db.import_jsonl(self.db_path, self.jsonl), 2)
        self.assertEqual(self.table_counts(),
                         {"runs": 2, "node_results": 0, "ip_profiles": 0})

    def test_old_format_imports_without_crash(self):
        self.dump_records([OLD_RECORD])
        self.assertEqual(db.import_jsonl(self.db_path, self.jsonl), 1)
        rows = self.query("SELECT name, jitter_ms, multi_mbps, sample_mb"
                          " FROM node_results ORDER BY id")
        self.assertEqual([r[0] for r in rows], ["旧节点A", "旧节点B"])
        self.assertEqual(rows[0][1:], (None, None, None))  # 旧格式缺的新字段存 NULL
        ips = self.query("SELECT name, kind, ok, proxy, hosting, mobile FROM ip_profiles")
        self.assertEqual(len(ips), 1)                      # ip={} 的旧节点B 不出画像行
        # ok 按 exit_ip 存在推断为 1；proxy/hosting/mobile 旧格式没有，保持 NULL
        self.assertEqual(ips[0], ("旧节点A", "住宅", 1, None, None, None))
        # raw 回放逐字段保真：risk 等老字段原样通过
        self.assertEqual(db.latest_run(self.db_path), OLD_RECORD)

    def test_incremental_import_appends_only_new(self):
        self.dump_records([make_record(ts_before(hours=2), [make_result("节点A")]),
                           make_record(ts_before(hours=1), [make_result("节点A")])])
        self.assertEqual(db.import_jsonl(self.db_path, self.jsonl), 2)
        with self.jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(make_record(ts_before(), [make_result("节点A")]),
                               ensure_ascii=False) + "\n")
        self.assertEqual(db.import_jsonl(self.db_path, self.jsonl), 1)
        self.assertEqual(self.table_counts()["runs"], 3)

    def test_missing_jsonl_returns_zero(self):
        self.assertEqual(db.import_jsonl(self.db_path, self.jsonl), 0)  # 文件不存在
        self.assertEqual(self.table_counts()["runs"], 0)


class LatestAndAllRunsTest(DbCase):
    def test_latest_run_matches_last_jsonl_line_field_by_field(self):
        recs = [make_record(ts_before(hours=2), [make_result("节点A")]),
                OLD_RECORD,
                make_record(ts_before(), [make_result("节点X", median=77.7)],
                            mb=5, rounds=2)]
        self.dump_records(recs)
        db.import_jsonl(self.db_path, self.jsonl)
        with self.jsonl.open(encoding="utf-8") as f:
            last_line = [l for l in f if l.strip()][-1]
        latest = db.latest_run(self.db_path)
        self.assertEqual(latest, json.loads(last_line))  # 与 jsonl 末行逐字段一致
        self.assertEqual(latest, recs[-1])

    def test_latest_run_empty_db_returns_empty_dict(self):
        db.import_jsonl(self.db_path, self.jsonl)  # 文件不存在：建空库
        self.assertEqual(db.latest_run(self.db_path), {})

    def test_all_runs_order_and_count(self):
        recs = [make_record(ts_before(hours=3), [make_result("节点A")], mb=10),
                OLD_RECORD,
                make_record(ts_before(), [make_result("节点B")], mb=30)]
        self.dump_records(recs)
        db.import_jsonl(self.db_path, self.jsonl)
        runs = db.all_runs(self.db_path)
        self.assertEqual(len(runs), 3)
        self.assertEqual(runs, recs)  # 按写入先后升序，每项逐字段一致

    def test_all_runs_empty_db(self):
        self.assertEqual(db.all_runs(self.db_path), [])


class NodeSeriesTest(DbCase):
    SERIES_KEYS = {"ts", "median_mbps", "best_mbps", "multi_mbps",
                   "latency_ms", "jitter_ms", "connect_ms", "score", "status"}

    def _import(self, recs):
        self.dump_records(recs)
        db.import_jsonl(self.db_path, self.jsonl)

    def test_days_window_filters_old_runs(self):
        self._import([
            make_record(ts_before(days=40), [make_result("节点A", median=10.0)]),
            make_record(ts_before(hours=2), [make_result("节点A", median=20.0)]),
            make_record(ts_before(hours=1), [make_result("节点B", median=30.0)]),
        ])
        rows30 = db.node_series(self.db_path, "节点A", days=30)
        self.assertEqual([r["median_mbps"] for r in rows30], [20.0])  # 40 天前被过滤
        rows60 = db.node_series(self.db_path, "节点A", days=60)
        self.assertEqual([r["median_mbps"] for r in rows60], [10.0, 20.0])
        self.assertLess(rows60[0]["ts"], rows60[1]["ts"])  # 时间升序

    def test_series_fields_complete(self):
        self._import([make_record(
            ts_before(),
            [make_result("节点A", median=42.5, latency=87, jitter=3.5, score=71.2)])])
        rows = db.node_series(self.db_path, "节点A", days=30)
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), self.SERIES_KEYS)
        row = rows[0]
        self.assertEqual((row["median_mbps"], row["best_mbps"], row["multi_mbps"]),
                         (42.5, 44.5, 85.0))
        self.assertEqual((row["latency_ms"], row["jitter_ms"], row["connect_ms"]),
                         (87, 3.5, 88.5))
        self.assertEqual((row["score"], row["status"]), (71.2, "ok"))

    def test_unknown_node_returns_empty(self):
        self._import([make_record(ts_before(), [make_result("节点A")])])
        self.assertEqual(db.node_series(self.db_path, "不存在节点", days=30), [])

    def test_days_clamped_to_valid_range(self):
        self._import([
            make_record(ts_before(days=40), [make_result("节点A")]),
            make_record(ts_before(hours=2), [make_result("节点A")]),
        ])
        # days=0 钳到 1 天：40 天前仍被过滤，2 小时前保留
        self.assertEqual(len(db.node_series(self.db_path, "节点A", days=0)), 1)
        # days=99999 钳到 3650 天：全部保留
        self.assertEqual(len(db.node_series(self.db_path, "节点A", days=99999)), 2)


class IpChangesTest(DbCase):
    CHANGE_KEYS = {"ts", "exit_ip", "country", "country_code", "isp", "org",
                   "asn", "asname", "kind", "proxy", "hosting", "mobile"}

    def _import(self, recs):
        self.dump_records(recs)
        db.import_jsonl(self.db_path, self.jsonl)

    def test_change_points_for_ip_and_asn(self):
        self._import([
            make_record(ts_before(hours=4), [make_result("节点A", exit_ip="203.0.113.1", asn="AS1 Net")]),
            make_record(ts_before(hours=3), [make_result("节点A", exit_ip="203.0.113.1", asn="AS1 Net")]),  # 不变：合并
            make_record(ts_before(hours=2), [make_result("节点A", exit_ip="203.0.113.2", asn="AS1 Net")]),  # 换 IP
            make_record(ts_before(hours=1), [make_result("节点A", exit_ip="203.0.113.2", asn="AS2 Net")]),  # 换 ASN
        ])
        tl = db.ip_changes(self.db_path, "节点A")
        self.assertEqual([(e["exit_ip"], e["asn"]) for e in tl],
                         [("203.0.113.1", "AS1 Net"),
                          ("203.0.113.2", "AS1 Net"),
                          ("203.0.113.2", "AS2 Net")])
        # 变化点取该组合首次出现的 ts，整体时间升序
        self.assertEqual([e["ts"] for e in tl],
                         [ts_before(hours=4), ts_before(hours=2), ts_before(hours=1)])
        self.assertEqual(set(tl[0]), self.CHANGE_KEYS)

    def test_no_change_collapses_to_single_entry(self):
        self._import([make_record(ts_before(hours=h), [make_result("节点A")])
                      for h in (3, 2, 1)])
        tl = db.ip_changes(self.db_path, "节点A")
        self.assertEqual(len(tl), 1)
        self.assertEqual(tl[0]["exit_ip"], "203.0.113.1")
        # 布尔标记从 0/1 还原为 True/False
        self.assertIs(tl[0]["proxy"], False)
        self.assertIs(tl[0]["hosting"], True)
        self.assertIs(tl[0]["mobile"], False)

    def test_failed_rounds_do_not_participate(self):
        self._import([
            make_record(ts_before(hours=5), [make_result("节点A", exit_ip="203.0.113.1", asn="AS1 Net")]),
            make_record(ts_before(hours=4), [make_result("节点A", median=None, exit_ip=None)]),  # 查询失败
            make_record(ts_before(hours=3), [make_result("节点A", exit_ip="203.0.113.1", asn="AS1 Net")]),
            make_record(ts_before(hours=2), [make_result("节点A", exit_ip="203.0.113.2", asn="AS1 Net")]),
            make_record(ts_before(hours=1), [make_result("节点B", median=None, exit_ip=None)]),  # 节点B 一直失败
        ])
        # 失败轮次（无 exit_ip）不参与：既不算「换了 IP」，也不打断相邻去重
        tl = db.ip_changes(self.db_path, "节点A")
        self.assertEqual([(e["exit_ip"], e["asn"]) for e in tl],
                         [("203.0.113.1", "AS1 Net"), ("203.0.113.2", "AS1 Net")])
        self.assertEqual(db.ip_changes(self.db_path, "节点B"), [])  # 全失败：空时间线

    def test_unknown_node_returns_empty(self):
        self._import([make_record(ts_before(), [make_result("节点A")])])
        self.assertEqual(db.ip_changes(self.db_path, "不存在节点"), [])

    def test_old_format_flags_stay_null(self):
        self._import([OLD_RECORD])
        tl = db.ip_changes(self.db_path, "旧节点A")
        self.assertEqual(len(tl), 1)
        self.assertEqual(tl[0]["kind"], "住宅")
        self.assertEqual(tl[0]["asn"], "AS13335 Cloudflare, Inc.")
        # 旧格式没有 proxy/hosting/mobile：保持 None 而非 False
        self.assertIsNone(tl[0]["proxy"])
        self.assertIsNone(tl[0]["hosting"])
        self.assertIsNone(tl[0]["mobile"])


if __name__ == "__main__":
    unittest.main()
