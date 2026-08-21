# -*- coding: utf-8 -*-
"""result_to_dict 新字段 + 历史记录兼容性：
- 新格式字段齐全、无 risk 残留
- 旧格式 jsonl（含 risk、kind="住宅"、缺新字段）读取方不崩：
  speedbench_web.read_history / latest_record / slim_history 与 speedbench_switch.load_best_name
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clash_speedbench as csb
import speedbench_switch as sbs
import speedbench_web as sbw

# 旧格式（v0.2 及更早）：有 risk 字段、ip.kind 为「住宅」，没有 jitter_ms/connect_ms/
# multi_mbps/sample_mb/samples_mbps，ip 里没有 asname/country_code/布尔标记/ok。
OLD_RECORD = {
    "ts": "2026-08-20T10:00:00",
    "mb": 20,
    "rounds": 2,
    "csv": "clash-speedtest-20260820.csv",
    "results": [
        {
            "name": "旧节点A", "provider": "机场甲", "proto": "ss",
            "latency_ms": 120, "median_mbps": 45.5, "best_mbps": 50.1,
            "score": 66.6, "stars": "★★★☆☆",
            "tags": "低延迟,住宅", "status": "ok", "risk": "低",
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


def full_result():
    ip = csb.IpInfo(exit_ip="203.0.113.9", country="美国", country_code="US",
                    region="California", city="LA", isp="Example ISP",
                    org="Example Org", asn="AS64496 Example Net",
                    asname="EXAMPLE-NET", kind="机房托管",
                    hosting=True, ok=True)
    r = csb.Result(name="节点X", provider="p", proto="hysteria2",
                   latency_ms=120, speeds_mbps=[40.0, 50.0],
                   median_mbps=45.123456, best_mbps=50.0, status="ok",
                   ip=ip, score=66.6, tags="低延迟,机房托管",
                   jitter_ms=8.24, connect_ms=155.5,
                   multi_mbps=123.4567, sample_mb=30)
    return r


class ResultToDictTest(unittest.TestCase):
    def test_new_fields_all_present(self):
        d = csb.result_to_dict(full_result())
        expected_top = {"name", "provider", "proto", "latency_ms", "jitter_ms",
                        "connect_ms", "median_mbps", "multi_mbps", "best_mbps",
                        "sample_mb", "samples_mbps", "score", "stars", "tags",
                        "status", "ip"}
        self.assertEqual(set(d), expected_top)
        expected_ip = {"exit_ip", "country", "country_code", "region", "city",
                       "isp", "org", "asn", "asname", "kind",
                       "proxy", "hosting", "mobile", "ok"}
        self.assertEqual(set(d["ip"]), expected_ip)

    def test_values_and_rounding(self):
        d = csb.result_to_dict(full_result())
        self.assertEqual(d["median_mbps"], 45.123)     # round 3
        self.assertEqual(d["multi_mbps"], 123.457)
        self.assertEqual(d["jitter_ms"], 8.24)
        self.assertEqual(d["connect_ms"], 155.5)
        self.assertEqual(d["sample_mb"], 30)
        self.assertEqual(d["samples_mbps"], [40.0, 50.0])
        self.assertEqual(d["ip"]["kind"], "机房托管")
        self.assertTrue(d["ip"]["hosting"])
        self.assertTrue(d["ip"]["ok"])

    def test_no_risk_no_residential_keys(self):
        d = csb.result_to_dict(full_result())
        blob = json.dumps(d, ensure_ascii=False)
        self.assertNotIn("risk", d)
        self.assertNotIn("risk", d["ip"])
        self.assertNotIn('"risk"', blob)
        self.assertNotIn("住宅", blob)

    def test_ip_none_uses_empty_defaults(self):
        r = full_result()
        r.ip = None
        d = csb.result_to_dict(r)
        self.assertFalse(d["ip"]["ok"])
        self.assertEqual(d["ip"]["kind"], "")
        self.assertEqual(d["ip"]["exit_ip"], "")


class HistoryCompatTest(unittest.TestCase):
    def write_history(self, tmpdir, lines):
        p = Path(tmpdir) / "speedbench-history.jsonl"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def test_web_reads_old_format_without_crash(self):
        with tempfile.TemporaryDirectory() as td:
            hist = self.write_history(td, [json.dumps(OLD_RECORD, ensure_ascii=False),
                                           "{这不是合法JSON", ""])
            with mock.patch.object(sbw, "HISTORY", hist):
                records = sbw.read_history()
                self.assertEqual(len(records), 1)          # 坏行被跳过
                latest = sbw.latest_record()
                self.assertEqual(latest["results"][0]["name"], "旧节点A")
                slim = sbw.slim_history()
        self.assertEqual(len(slim), 1)
        self.assertEqual(slim[0]["ts"], "2026-08-20T10:00:00")
        self.assertEqual(
            slim[0]["results"],
            [
                {"name": "旧节点A", "median_mbps": 45.5, "latency_ms": 120, "score": 66.6},
                {"name": "旧节点B", "median_mbps": None, "latency_ms": None, "score": 0},
            ])

    def test_slim_history_tolerates_record_without_results(self):
        with tempfile.TemporaryDirectory() as td:
            hist = self.write_history(td, [json.dumps({"ts": "2026-08-19T09:00:00"})])
            with mock.patch.object(sbw, "HISTORY", hist):
                slim = sbw.slim_history()
        self.assertEqual(slim, [{"ts": "2026-08-19T09:00:00", "results": []}])

    def test_web_reads_empty_history(self):
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td) / "不存在.jsonl"
            with mock.patch.object(sbw, "HISTORY", hist):
                self.assertEqual(sbw.read_history(), [])
                self.assertEqual(sbw.latest_record(), {})
                self.assertEqual(sbw.slim_history(), [])

    def test_switch_load_best_from_old_format(self):
        with tempfile.TemporaryDirectory() as td:
            hist = self.write_history(td, [json.dumps(OLD_RECORD, ensure_ascii=False)])
            self.assertEqual(sbs.load_best_name(hist), "旧节点A")

    def test_switch_load_best_rejects_zero_score(self):
        rec = json.loads(json.dumps(OLD_RECORD))
        rec["results"][0]["score"] = 0
        with tempfile.TemporaryDirectory() as td:
            hist = self.write_history(td, [json.dumps(rec, ensure_ascii=False)])
            with self.assertRaises(csb.ApiError):
                sbs.load_best_name(hist)

    def test_new_format_roundtrip_through_web_reader(self):
        """append_history 写出的新格式记录，Web 端能读且新字段在。"""
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td) / "h.jsonl"
            csb.append_history([full_result()], hist, mb=None, rounds=2, csv_path=None)
            with mock.patch.object(sbw, "HISTORY", hist):
                latest = sbw.latest_record()
                slim = sbw.slim_history()
        r0 = latest["results"][0]
        self.assertEqual(r0["name"], "节点X")
        self.assertEqual(r0["sample_mb"], 30)
        self.assertEqual(r0["ip"]["kind"], "机房托管")
        self.assertIn("jitter_ms", r0)
        self.assertIn("connect_ms", r0)
        self.assertNotIn("risk", r0)
        self.assertEqual(slim[0]["results"][0]["median_mbps"], 45.123)


if __name__ == "__main__":
    unittest.main()
