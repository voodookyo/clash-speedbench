# -*- coding: utf-8 -*-
"""Local WebRTC/DNS leak audit SQLite persistence tests."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import speedbench_db as db


class LeakAuditDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "history.db"

    def _restore_env(self, old):
        if old is None:
            os.environ.pop("SPEEDBENCH_IPQS_KEY", None)
        else:
            os.environ["SPEEDBENCH_IPQS_KEY"] = old

    def query(self, sql, params=()):
        conn = sqlite3.connect(str(self.db_path))
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def test_insert_and_read_audit_with_nested_local_observations(self):
        audit = {
            "created_at": 1724880000,
            "exit_ipv4": "198.51.100.10",
            "exit_ipv6": None,
            "webrtc_status": "inconclusive",
            "candidates": [
                {"type": "host", "address": "192.168.1.2", "public": False},
                {"type": "srflx", "address": "198.51.100.10", "public": True},
            ],
            "dns_mode": "guided",
            "dns_status": "user_review",
            "details": {"note": "BrowserLeaks opened", "leak": False},
        }
        row_id = db.insert_leak_audit(self.db_path, audit)
        self.assertIsInstance(row_id, int)
        rows = db.leak_audits(self.db_path)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], row_id)
        self.assertEqual(row["created_at"], 1724880000)
        self.assertEqual(row["webrtc_status"], "inconclusive")
        self.assertEqual(row["candidates"][0]["type"], "host")
        self.assertEqual(row["dns_mode"], "guided")
        self.assertEqual(row["details"]["note"], "BrowserLeaks opened")

    def test_dataclass_like_to_dict_and_bad_created_at_are_supported(self):
        class Audit:
            def to_dict(self):
                return {
                    "created_at": "not-a-timestamp",
                    "webrtc": {"status": "unknown", "candidates": []},
                    "dns": {"mode": "guided", "status": None},
                }
        row_id = db.insert_leak_audit(self.db_path, Audit())
        self.assertGreater(row_id, 0)
        row = db.leak_audits(self.db_path)[0]
        self.assertEqual(row["webrtc_status"], "unknown")
        self.assertEqual(row["dns_mode"], "guided")
        self.assertEqual(row["candidates"], [])

    def test_limit_is_clamped_and_order_is_newest_first(self):
        for timestamp in (10, 30, 20):
            db.insert_leak_audit(self.db_path, {
                "created_at": timestamp, "webrtc_status": "unknown",
                "candidates": [], "dns_mode": "guided",
            })
        self.assertEqual([row["created_at"] for row in db.leak_audits(self.db_path, 2)],
                         [30, 20])
        self.assertEqual(len(db.leak_audits(self.db_path, "invalid")), 3)
        self.assertEqual(len(db.leak_audits(self.db_path, 0)), 1)

    def test_secrets_and_credential_fields_are_not_persisted_or_returned(self):
        sentinel = "secret-sentinel-leak"
        old = os.environ.get("SPEEDBENCH_IPQS_KEY")
        os.environ["SPEEDBENCH_IPQS_KEY"] = sentinel
        self.addCleanup(self._restore_env, old)
        row_id = db.insert_leak_audit(self.db_path, {
            "created_at": 40,
            "webrtc_status": "unknown",
            "candidates": [{"address": "198.51.100.20",
                            "api_key": sentinel,
                            "token": sentinel}],
            "dns_mode": "guided",
            "details": {"authorization": sentinel, "message": "local only"},
        })
        self.assertGreater(row_id, 0)
        raw = self.query("SELECT candidates_json, details_json FROM leak_audits")[0]
        self.assertNotIn(sentinel, " ".join(raw))
        self.assertNotIn("api_key", raw[0])
        self.assertNotIn("token", raw[0])
        self.assertNotIn("authorization", raw[1])
        returned = json.dumps(db.leak_audits(self.db_path), ensure_ascii=False)
        self.assertNotIn(sentinel, returned)
        self.assertNotIn("api_key", returned)
        self.assertNotIn("token", returned)

    def test_parameterized_malicious_detail_does_not_drop_schema(self):
        marker = "x'); DROP TABLE leak_audits;--"
        db.insert_leak_audit(self.db_path, {
            "created_at": 50, "webrtc_status": marker,
            "candidates": [{"address": marker}], "dns_mode": marker,
            "details": {"message": marker},
        })
        self.assertEqual(len(db.leak_audits(self.db_path)), 1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM leak_audits"), [(1,)])


if __name__ == "__main__":
    unittest.main()
