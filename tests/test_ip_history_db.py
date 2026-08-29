# -*- coding: utf-8 -*-
"""SQLite IP Intelligence history and migration coverage.

These tests deliberately use JSON fixtures only.  They exercise the additive
database boundary without contacting any provider and keep the JSONL replay
contract visible: ``runs.raw`` remains the source text, while normalized
intelligence is indexed in its own table.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import speedbench_db as db


def result(name, *, node_key="node-key", ip="203.0.113.10", intel=None,
           alias="intel_v4", score=80.0):
    value = {
        "name": name,
        "node_key": node_key,
        "provider": "fixture-provider",
        "proto": "ss",
        "status": "ok",
        "median_mbps": 100.0,
        "latency_ms": 50.0,
        "score": score,
        "ip": {
            "exit_ip": ip,
            "country": "United States",
            "country_code": "US",
            "isp": "Example ISP",
            "org": "Example ISP",
            "asn": "AS64500 Example",
            "asname": "EXAMPLE",
            "proxy": False,
            "hosting": False,
            "mobile": False,
            "ok": True,
        },
        "probe_attempts": 5,
        "probe_successes": 4,
        "probe_failures": 1,
        "probe_success_rate": 0.8,
        "probe_loss_pct": 20.0,
        "network_score": 72.5,
        "ip_quality_score": None,
        "ip_grade": None,
    }
    if intel is not None:
        value[alias] = dict(intel)
    return value


def record(ts, results):
    return {"ts": ts, "mb": 20, "rounds": 1, "results": results}


class IpHistoryDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db_path = root / "history.db"
        self.jsonl = root / "history.jsonl"

    def write(self, records, *, exact_lines=False):
        if exact_lines:
            text = "\n".join(records) + "\n"
        else:
            text = "".join(json.dumps(item, ensure_ascii=False) + "\n"
                           for item in records)
        self.jsonl.write_text(text, encoding="utf-8")

    def query(self, sql, params=()):
        conn = sqlite3.connect(str(self.db_path))
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def test_fresh_schema_and_additive_node_columns(self):
        self.write([record("2026-08-29T00:00:00", [result("node")])])
        self.assertEqual(db.import_jsonl(self.db_path, self.jsonl), 1)
        tables = {row[0] for row in self.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"ip_intel_cache", "ip_intel_results", "leak_audits"}
                        <= tables)
        columns = {row[1] for row in self.query("PRAGMA table_info(node_results)")}
        self.assertTrue({
            "probe_attempts", "probe_successes", "probe_failures",
            "probe_success_rate", "probe_loss_pct", "network_score",
            "ip_quality_score", "ip_grade", "exit_ipv4", "exit_ipv6",
        } <= columns)
        self.assertEqual(self.query(
            "SELECT probe_attempts, probe_successes, probe_failures, "
            "probe_success_rate, probe_loss_pct, network_score "
            "FROM node_results"), [(5, 4, 1, 0.8, 20.0, 72.5)])

    def test_import_deduplicates_same_ip_per_run_and_accepts_alias(self):
        intel = {
            "ip": "203.0.113.10",
            "ip_version": 4,
            "classification": {"category": "residential", "confidence": 90,
                                "evidence": ["IPQS Residential"], "conflicts": []},
            "ip_quality_score": 91.0,
            "ip_grade": "S",
            "ipqs_fraud_score": 4,
            "scamalytics_score": 5,
            "provider_status": {"ipqs": "ok", "scamalytics": "cache_hit"},
            # This must never be copied to normalized_json.
            "provider_data": {"ipqs": {"api_key": "secret-sentinel"}},
        }
        self.write([record("2026-08-29T00:01:00", [
            result("node-a", intel=intel),
            result("node-b", node_key="node-b-key", intel=intel,
                   alias="ip_intel_v4"),
        ])])
        self.assertEqual(db.import_jsonl(self.db_path, self.jsonl), 1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM ip_intel_results"), [(1,)])
        row = self.query(
            "SELECT exit_ip, ip_version, classification, confidence, "
            "ip_quality_score, ip_grade, ipqs_fraud_score, scamalytics_score, "
            "evidence_json, provider_status_json, normalized_json "
            "FROM ip_intel_results")[0]
        self.assertEqual(row[:8], ("203.0.113.10", 4, "residential", 90,
                                   91.0, "S", 4, 5))
        self.assertNotIn("secret-sentinel", row[-1])
        self.assertNotIn("api_key", row[-1])
        self.assertEqual(len(json.loads(row[8])), 1)

    def test_old_schema_migrates_idempotently_and_old_fields_are_null(self):
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript("""
                CREATE TABLE runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL UNIQUE, mb REAL, rounds INTEGER,
                    node_count INTEGER NOT NULL DEFAULT 0, raw TEXT NOT NULL);
                CREATE TABLE node_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL, name TEXT NOT NULL,
                    proto TEXT, provider TEXT, latency_ms REAL,
                    jitter_ms REAL, connect_ms REAL, median_mbps REAL,
                    best_mbps REAL, multi_mbps REAL, sample_mb REAL,
                    score REAL, stars TEXT, status TEXT, tags TEXT);
                CREATE TABLE ip_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL, name TEXT NOT NULL,
                    exit_ip TEXT, country TEXT, country_code TEXT,
                    isp TEXT, org TEXT, asn TEXT, asname TEXT, kind TEXT,
                    ok INTEGER, proxy INTEGER, hosting INTEGER, mobile INTEGER);
            """)
            conn.execute(
                "INSERT INTO runs(ts, raw, node_count) VALUES (?,?,?)",
                ("2026-08-20T00:00:00", "{}", 1))
            run_id = conn.execute("SELECT id FROM runs").fetchone()[0]
            conn.execute(
                "INSERT INTO node_results(run_id,name,status) VALUES (?,?,?)",
                (run_id, "old-node", "ok"))
            conn.commit()
        finally:
            conn.close()
        self.write([])
        self.assertEqual(db.import_jsonl(self.db_path, self.jsonl), 0)
        columns = {row[1] for row in self.query("PRAGMA table_info(node_results)")}
        self.assertIn("probe_loss_pct", columns)
        self.assertIn("ip_quality_score", columns)
        self.assertEqual(self.query(
            "SELECT probe_attempts, probe_loss_pct, ip_quality_score, ip_grade "
            "FROM node_results"), [(None, None, None, None)])
        # Reopening must not produce duplicate columns or fail on the new tables.
        self.assertEqual(db.import_jsonl(self.db_path, self.jsonl), 0)

    def test_reputation_change_marks_same_ip_residential_proxy_and_score_drop(self):
        first = {
            "ip": "203.0.113.50", "ip_version": 4,
            "classification": {"category": "residential", "confidence": 90},
            "ip_quality_score": 92, "ip_grade": "S",
            "ipqs_fraud_score": 8, "scamalytics_score": 10,
            "provider_status": {"ipqs": "ok"},
        }
        second = {
            "ip": "203.0.113.50", "ip_version": 4,
            "classification": {"category": "residential_proxy", "confidence": 84},
            "ip_quality_score": 35, "ip_grade": "D",
            "ipqs_fraud_score": 71, "scamalytics_score": 62,
            "provider_status": {"ipqs": "ok", "scamalytics": "ok"},
        }
        self.write([
            record("2026-08-29T00:02:00", [
                result("node", ip="203.0.113.50", intel=first)
            ]),
            record("2026-08-29T00:03:00", [
                result("node", ip="203.0.113.50", intel=second)
            ]),
        ])
        self.assertEqual(db.import_jsonl(self.db_path, self.jsonl), 2)
        changes = db.ip_reputation_changes(self.db_path, "node")
        self.assertEqual(len(changes), 2)
        self.assertEqual(changes[0]["classification"], "residential")
        self.assertEqual(changes[1]["classification"], "residential_proxy")
        self.assertEqual(changes[1]["ipqs_fraud_score"], 71)
        self.assertTrue(changes[1]["reputation_worsened"])
        self.assertTrue(changes[1]["same_ip_reputation_worsened"])

    def test_dual_stack_timeline_keeps_family_ips_and_compares_reputation_by_ip(self):
        v4_clean = {
            "ip": "198.51.100.40", "ip_version": 4,
            "classification": {"category": "residential", "confidence": 90},
            "ip_quality_score": 92, "ip_grade": "S",
            "ipqs_fraud_score": 8, "scamalytics_score": 9,
        }
        v4_worse = {
            "ip": "198.51.100.40", "ip_version": 4,
            "classification": {"category": "residential_proxy", "confidence": 84},
            "ip_quality_score": 35, "ip_grade": "D",
            "ipqs_fraud_score": 72, "scamalytics_score": 63,
        }
        v6_stable = {
            "ip": "2001:db8::40", "ip_version": 6,
            "classification": {"category": "corporate", "confidence": 70},
            "ip_quality_score": 80, "ip_grade": "A",
            "ipqs_fraud_score": 12, "scamalytics_score": 14,
        }

        def dual_result(v4):
            item = result("dual", ip="198.51.100.40", intel=v4)
            item["exit_ipv4"] = "198.51.100.40"
            item["exit_ipv6"] = "2001:db8::40"
            item["intel_v6"] = dict(v6_stable)
            return item

        self.write([
            record("2026-08-29T00:06:00", [dual_result(v4_clean)]),
            record("2026-08-29T00:07:00", [dual_result(v4_worse)]),
        ])
        self.assertEqual(db.import_jsonl(self.db_path, self.jsonl), 2)
        changes = db.ip_reputation_changes(self.db_path, "dual")
        self.assertEqual(
            [item["exit_ip"] for item in changes],
            ["198.51.100.40", "2001:db8::40",
             "198.51.100.40", "2001:db8::40"],
        )
        self.assertEqual(changes[0]["exit_ipv4"], "198.51.100.40")
        self.assertEqual(changes[0]["exit_ipv6"], "2001:db8::40")
        self.assertEqual(changes[1]["exit_ip"], "2001:db8::40")
        self.assertEqual(changes[2]["ip_grade"], "D")
        self.assertTrue(changes[2]["reputation_worsened"])
        # The v4 comparison must use v4's previous S grade, not the
        # immediately preceding v6 A grade.
        self.assertEqual(changes[2]["ipqs_fraud_score"], 72)

    def test_legacy_raw_replay_and_missing_intelligence_are_safe(self):
        legacy = {
            "ts": "2026-08-29T00:04:00", "mb": 1,
            "results": [{"name": "old", "ip": {"exit_ip": "198.51.100.7",
                                                   "asn": "AS7"}}],
        }
        # Leading spaces are retained in runs.raw while playback remains equal.
        raw = "  " + json.dumps(legacy, ensure_ascii=False) + "  "
        self.write([raw], exact_lines=True)
        self.assertEqual(db.import_jsonl(self.db_path, self.jsonl), 1)
        self.assertEqual(db.latest_run(self.db_path), legacy)
        self.assertEqual(db.all_runs(self.db_path), [legacy])
        self.assertEqual(self.query("SELECT raw FROM runs")[0][0], raw)
        changes = db.ip_reputation_changes(self.db_path, "old")
        self.assertEqual(len(changes), 1)
        self.assertFalse(changes[0]["intel_available"])
        self.assertIsNone(changes[0]["ip_quality_score"])

    def test_malicious_json_values_are_parameters_not_sql(self):
        marker = "x'); DROP TABLE runs;--"
        self.write([record("2026-08-29T00:05:00", [
            result(marker, ip=marker, intel={
                "ip": marker,
                "classification": marker,
                "evidence": [marker],
                "provider_status": {marker: marker},
            }),
        ])])
        self.assertEqual(db.import_jsonl(self.db_path, self.jsonl), 1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM runs"), [(1,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM ip_intel_results"), [(1,)])
        self.assertEqual(len(db.ip_reputation_changes(self.db_path, marker)), 1)


if __name__ == "__main__":
    unittest.main()
