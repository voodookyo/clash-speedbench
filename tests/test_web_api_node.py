# -*- coding: utf-8 -*-
"""Web API v0.5（读接口改走 SQLite）HTTP 级测试。

- GET /api/node：series/ip_changes 结构、days 窗口与非数字容错、缺 name 400、
  SQL 注入尝试（' OR 1=1--）返回空结构而不是报错或泄露全表
- GET /api/latest：patch HISTORY 指向临时 jsonl 后，响应与末行逐字段一致（走 sync_db）
- GET /api/history：slim 结构不回归

HISTORY 一律 patch 到 TemporaryDirectory（db_path 随之指向临时目录），
sync_db 的 mtime 缓存按 db 路径为 key，各用例路径唯一互不串扰；不碰仓库真实数据。
"""
import json
import sys
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import speedbench_web as web
from tests.web_server_case import WebServerCase

_TS_FMT = "%Y-%m-%dT%H:%M:%S"
_BASE = datetime.now()


def ts_before(**kw):
    return (_BASE - timedelta(**kw)).strftime(_TS_FMT)


def make_result(name, median=50.0, latency=100, score=60.0,
                exit_ip="203.0.113.1", asn="AS1 Net"):
    r = {"name": name, "latency_ms": latency, "median_mbps": median,
         "score": score, "status": "ok" if median is not None else "timeout"}
    r["ip"] = ({"exit_ip": exit_ip, "country_code": "SG", "asn": asn, "ok": True}
               if exit_ip else {"ok": False})
    return r


def make_record(ts, results, mb=30, rounds=1):
    return {"ts": ts, "mb": mb, "rounds": rounds, "results": results}


OLD_RECORD = {
    "ts": "2026-08-20T10:00:00",
    "mb": 20,
    "rounds": 2,
    "results": [
        {"name": "旧节点A", "latency_ms": 120, "median_mbps": 45.5,
         "score": 66.6, "status": "ok", "risk": "低",
         "ip": {"exit_ip": "203.0.113.9", "country": "美国",
                "asn": "AS13335 Cloudflare, Inc.", "kind": "住宅", "risk": "低"}},
    ],
}


class WebApiCase(WebServerCase):
    """每个用例：独立端口 server + 独立临时目录里的 jsonl/db。"""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hist = Path(self._tmp.name) / "speedbench-history.jsonl"

    def write_history(self, records):
        self.hist.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
            encoding="utf-8")

    def get_json(self, path):
        """patch HISTORY 后发 GET，返回 (status, 解析后的 JSON)。"""
        with mock.patch.object(web, "HISTORY", self.hist):
            status, body = self.request("GET", path)
        return status, json.loads(body.decode("utf-8"))

    @staticmethod
    def node_path(name=None, days=None):
        qs = {}
        if name is not None:
            qs["name"] = name
        if days is not None:
            qs["days"] = days
        return "/api/node" + ("?" + urllib.parse.urlencode(qs) if qs else "")


class ApiNodeTest(WebApiCase):
    def _history(self):
        """节点A：40 天前 / 3 小时前 / 2 小时前失败轮 / 1 小时前换 IP；另有节点B。"""
        return [
            make_record(ts_before(days=40), [make_result("节点A", median=10.0, exit_ip="203.0.113.1")]),
            make_record(ts_before(hours=3), [make_result("节点A", median=20.0, exit_ip="203.0.113.1")]),
            make_record(ts_before(hours=2), [make_result("节点A", median=None, exit_ip=None)]),
            make_record(ts_before(hours=1), [make_result("节点A", median=30.0, exit_ip="203.0.113.2", asn="AS2 Net"),
                                             make_result("节点B", median=40.0)]),
        ]

    def test_node_returns_series_and_ip_changes(self):
        self.write_history(self._history())
        status, d = self.get_json(self.node_path("节点A", 60))
        self.assertEqual(status, 200)
        self.assertEqual(set(d), {"series", "ip_changes"})
        self.assertEqual([s["median_mbps"] for s in d["series"]],
                         [10.0, 20.0, None, 30.0])  # 失败轮次也在序列里（median 为 null）
        self.assertEqual(set(d["series"][0]),
                         {"ts", "median_mbps", "best_mbps", "multi_mbps",
                          "latency_ms", "jitter_ms", "connect_ms", "score", "status"})
        # 失败轮次（无 exit_ip）不进时间线；相邻不变合并，换 IP/ASN 产生变化点
        self.assertEqual([(e["exit_ip"], e["asn"]) for e in d["ip_changes"]],
                         [("203.0.113.1", "AS1 Net"), ("203.0.113.2", "AS2 Net")])

    def test_node_days_window_and_bad_days_fallback(self):
        self.write_history(self._history())
        _, d30 = self.get_json(self.node_path("节点A", 30))
        self.assertEqual([s["median_mbps"] for s in d30["series"]],
                         [20.0, None, 30.0])  # 40 天前被窗口过滤
        _, d60 = self.get_json(self.node_path("节点A", 60))
        self.assertEqual(len(d60["series"]), 4)
        # days 非数字：回退默认 30 天而不是 500
        status, dbad = self.get_json(self.node_path("节点A", "abc"))
        self.assertEqual(status, 200)
        self.assertEqual([s["median_mbps"] for s in dbad["series"]], [20.0, None, 30.0])

    def test_node_missing_name_400(self):
        self.write_history(self._history())
        for path in ("/api/node", "/api/node?name=", "/api/node?days=30"):
            with self.subTest(path=path):
                status, d = self.get_json(path)
                self.assertEqual(status, 400)
                self.assertFalse(d["ok"])

    def test_node_sql_injection_returns_empty_not_error(self):
        self.write_history(self._history())
        status, d = self.get_json(self.node_path("' OR 1=1--"))
        self.assertEqual(status, 200)
        self.assertEqual(d["series"], [])       # 参数化查询：注入串当普通名字，查不到
        self.assertEqual(d["ip_changes"], [])
        # 注入尝试不破坏后续正常查询
        status, d2 = self.get_json(self.node_path("节点A", 60))
        self.assertEqual(status, 200)
        self.assertEqual(len(d2["series"]), 4)

    def test_node_unknown_name_returns_empty(self):
        self.write_history(self._history())
        status, d = self.get_json(self.node_path("不存在节点"))
        self.assertEqual(status, 200)
        self.assertEqual(d, {"series": [], "ip_changes": []})

    def test_node_empty_history_returns_empty(self):
        # jsonl 不存在：sync_db 无可导入，接口仍返回 200 空结构
        status, d = self.get_json(self.node_path("节点A"))
        self.assertEqual(status, 200)
        self.assertEqual(d, {"series": [], "ip_changes": []})


class ApiLatestHistoryTest(WebApiCase):
    def test_latest_matches_last_jsonl_line(self):
        recs = [
            make_record(ts_before(hours=2), [make_result("节点A", median=11.1)], mb=10),
            OLD_RECORD,
            make_record(ts_before(),
                        [make_result("节点X", median=88.8, latency=66, score=77.7)],
                        mb=5, rounds=2),
        ]
        self.write_history(recs)
        status, d = self.get_json("/api/latest")
        self.assertEqual(status, 200)
        last = json.loads(self.hist.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(d, last)    # 与 jsonl 末行逐字段一致（raw 回放）
        self.assertEqual(d, recs[-1])

    def test_latest_includes_old_format_verbatim(self):
        self.write_history([OLD_RECORD])
        status, d = self.get_json("/api/latest")
        self.assertEqual(status, 200)
        self.assertEqual(d, OLD_RECORD)  # risk 等老字段原样通过

    def test_latest_empty_history_returns_empty_dict(self):
        status, d = self.get_json("/api/latest")
        self.assertEqual(status, 200)
        self.assertEqual(d, {})

    def test_history_slim_structure(self):
        recs = [
            make_record(ts_before(hours=2),
                        [make_result("节点A", median=11.1, latency=90, score=55.5),
                         make_result("节点B", median=None, latency=None, score=0)]),
            make_record(ts_before(),
                        [make_result("节点A", median=22.2, latency=80, score=66.6)]),
        ]
        self.write_history(recs)
        status, slim = self.get_json("/api/history")
        self.assertEqual(status, 200)
        self.assertEqual(slim, [
            {"ts": recs[0]["ts"],
             "results": [
                 {"name": "节点A", "median_mbps": 11.1, "latency_ms": 90, "score": 55.5},
                 {"name": "节点B", "median_mbps": None, "latency_ms": None, "score": 0}]},
            {"ts": recs[1]["ts"],
             "results": [
                 {"name": "节点A", "median_mbps": 22.2, "latency_ms": 80, "score": 66.6}]},
        ])

    def test_history_empty(self):
        status, slim = self.get_json("/api/history")
        self.assertEqual(status, 200)
        self.assertEqual(slim, [])


if __name__ == "__main__":
    unittest.main()
