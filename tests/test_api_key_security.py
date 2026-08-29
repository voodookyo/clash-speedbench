# -*- coding: utf-8 -*-
"""集中式 API credential regression tests.

These tests exercise the production serialization/persistence boundaries with
sentinel credentials, while replacing every provider transport and basic leak
lookup with local fixtures.  They intentionally build the JSONL through
``append_history``; a hand-edited legacy ``runs.raw`` line is a separate
compatibility contract and is not treated as an output-sanitization test.
"""
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clash_speedbench as csb
import speedbench_db as db
import speedbench_ip_intel as intel
import speedbench_web as web
from tests.web_server_case import WebServerCase


API_KEY = "speedbench-api-key-sentinel-7d6b"
SCAMALYTICS_USERNAME = "speedbench-scamalytics-user-sentinel-91a4"
IP = "198.51.100.77"


def _fixture_transport(url, timeout=None, headers=None):
    """Return official-response-shaped local fixtures; never opens a socket."""
    if "ipqualityscore.com" in url:
        return 200, {
            "success": True,
            "IP": IP,
            "ISP": "Example Consumer ISP",
            "organization": "Example Consumer ISP",
            "ASN": "AS64500",
            "connection_type": "Residential",
            "fraud_score": 4,
            "proxy": False,
            "vpn": False,
            "tor": False,
            "recent_abuse": False,
            # Simulate a provider echo without adding it to normalized data.
            "echo": API_KEY,
        }, {}
    if "scamalytics.com" in url:
        return 200, {
            "status": "ok",
            "scamalytics": {
                "ip": IP,
                "scamalytics_score": 3,
                "scamalytics_risk": "low",
                "account_echo": SCAMALYTICS_USERNAME,
                "scamalytics_proxy": {
                    "is_datacenter": False,
                    "is_proxy": False,
                },
            },
        }, {}
    raise AssertionError("unexpected provider fixture URL")


def _build_result():
    ipqs = intel.IpqsProvider(key=API_KEY, transport=_fixture_transport)
    scam = intel.ScamalyticsProvider(
        username=SCAMALYTICS_USERNAME,
        key=API_KEY,
        region="eu",
        transport=_fixture_transport,
    )
    ipqs_result = ipqs.query(IP)
    scam_result = scam.query(IP)
    assert ipqs_result.status == "ok"
    assert scam_result.status == "ok"
    intelligence = intel.aggregate_ip_intelligence(IP, {
        "ipqs": ipqs_result,
        "scamalytics": scam_result,
    })
    result = csb.Result(
        name="fixture-node",
        provider="fixture-subscription",
        proto="ss",
        latency_ms=80,
        speeds_mbps=[100.0],
        median_mbps=100.0,
        best_mbps=100.0,
        status="ok",
        ip=csb.IpInfo(
            exit_ip=IP,
            country="United States",
            country_code="US",
            isp="Example Consumer ISP",
            org="Example Consumer ISP",
            asn="AS64500 Example",
            asname="EXAMPLE",
            kind="ISP/非托管",
            ok=True,
        ),
        node_key="fixture-node-key",
        probe_attempts=3,
        probe_successes=3,
        probe_failures=0,
        probe_success_rate=100.0,
        probe_loss_pct=0.0,
        exit_ipv4=IP,
        intel_v4=intelligence,
        network_score=88.0,
    )
    csb.compute_score(result)
    return result, {"ipqs": ipqs_result, "scamalytics": scam_result}


class FormalSerializationSecurityTest(unittest.TestCase):
    def test_formal_jsonl_csv_and_sqlite_chain_never_persists_credentials(self):
        result, provider_results = _build_result()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jsonl = root / "history.jsonl"
            csv_path = root / "results.csv"
            db_path = root / "history.db"

            # The production path is result_to_dict -> append_history.  The
            # provider raw responses are intentionally not part of this
            # public result shape.
            payload = csb.result_to_dict(result)
            payload_blob = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn(API_KEY, payload_blob)
            self.assertNotIn(SCAMALYTICS_USERNAME, payload_blob)
            self.assertNotIn("provider_results", payload_blob)
            self.assertNotIn('"raw"', payload_blob)

            csb.append_history([result], jsonl, mb=1, rounds=1,
                               csv_path=csv_path)
            jsonl_blob = jsonl.read_text(encoding="utf-8")
            self.assertNotIn(API_KEY, jsonl_blob)
            self.assertNotIn(SCAMALYTICS_USERNAME, jsonl_blob)

            csb.write_csv([result], csv_path)
            csv_blob = csv_path.read_text(encoding="utf-8-sig")
            self.assertNotIn(API_KEY, csv_blob)
            self.assertNotIn(SCAMALYTICS_USERNAME, csv_blob)

            # Cache persistence is a separate formal boundary.  Supplying the
            # in-memory credential set also covers a custom provider whose raw
            # fixture contains a non-secret-shaped echo field.
            cache = intel.IpIntelCache(
                db_path,
                secrets=(API_KEY, SCAMALYTICS_USERNAME),
            )
            for provider_result in provider_results.values():
                cache.put(provider_result)

            self.assertEqual(db.import_jsonl(db_path, jsonl), 1)
            conn = sqlite3.connect(str(db_path))
            try:
                for table in ("runs", "node_results", "ip_profiles",
                              "ip_intel_cache", "ip_intel_results"):
                    rows = conn.execute("SELECT * FROM " + table).fetchall()
                    table_blob = repr(rows)
                    self.assertNotIn(API_KEY, table_blob, table)
                    self.assertNotIn(SCAMALYTICS_USERNAME, table_blob, table)

                # Be explicit about each persisted representation requested by
                # the acceptance contract.
                raw = conn.execute("SELECT raw FROM runs").fetchone()[0]
                normalized = conn.execute(
                    "SELECT normalized_json FROM ip_intel_results"
                ).fetchone()[0]
                cached = conn.execute(
                    "SELECT raw_json, normalized_json FROM ip_intel_cache"
                ).fetchall()
                node_row = conn.execute(
                    "SELECT provider, status, tags FROM node_results"
                ).fetchone()
                self.assertNotIn(API_KEY, raw)
                self.assertNotIn(SCAMALYTICS_USERNAME, raw)
                self.assertNotIn(API_KEY, normalized)
                self.assertNotIn(SCAMALYTICS_USERNAME, normalized)
                self.assertTrue(cached)
                self.assertNotIn(API_KEY, repr(cached))
                self.assertNotIn(SCAMALYTICS_USERNAME, repr(cached))
                self.assertNotIn(API_KEY, repr(node_row))
                self.assertNotIn(SCAMALYTICS_USERNAME, repr(node_row))
            finally:
                conn.close()


class CacheSingleFlightSecurityTest(unittest.TestCase):
    def test_same_ip_multiple_nodes_calls_provider_once(self):
        calls = []
        call_lock = threading.Lock()

        class CountingProvider:
            name = "ipqs"
            ttl_seconds = 86400

            def query(self, ip):
                with call_lock:
                    calls.append(ip)
                # Keep the overlap deterministic so all callers contend for
                # the same provider+IP flight rather than racing by accident.
                time.sleep(0.03)
                return intel.ProviderResult(
                    provider=self.name,
                    ip=ip,
                    status="ok",
                    normalized={"fraud_score": 4, "proxy": False},
                )

        with tempfile.TemporaryDirectory() as td:
            cache = intel.IpIntelCache(Path(td) / "history.db",
                                       secrets=(API_KEY,))
            provider = CountingProvider()
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(
                    lambda _unused: cache.get_or_query(provider, IP),
                    range(8),
                ))
            self.assertEqual(calls, [IP])
            self.assertTrue(all(item.ok for item in results))
            conn = sqlite3.connect(str(Path(td) / "history.db"))
            try:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM ip_intel_cache"
                ).fetchone()[0], 1)
            finally:
                conn.close()


class WebCredentialBoundaryTest(WebServerCase):
    def setUp(self):
        super().setUp()
        with web._IP_INTEL_SETTINGS_LOCK:
            self._old_overrides = dict(web._IP_INTEL_OVERRIDES)
            web._IP_INTEL_OVERRIDES.clear()
        self.addCleanup(self._restore_overrides)

    def _restore_overrides(self):
        with web._IP_INTEL_SETTINGS_LOCK:
            web._IP_INTEL_OVERRIDES.clear()
            web._IP_INTEL_OVERRIDES.update(self._old_overrides)

    def test_settings_status_and_http_responses_never_echo_credentials(self):
        settings = {
            "ipinfo_token": API_KEY,
            "ipqs_key": API_KEY,
            "scamalytics_username": SCAMALYTICS_USERNAME,
            "scamalytics_key": API_KEY,
            "scamalytics_region": "eu",
        }
        status, body = self.post_authorized("/api/ip-intel/settings", settings)
        self.assertEqual(status, 200)
        self.assertNotIn(API_KEY, body.decode("utf-8"))
        self.assertNotIn(SCAMALYTICS_USERNAME, body.decode("utf-8"))

        status, body = self.request("GET", "/api/ip-intel/status")
        self.assertEqual(status, 200)
        status_blob = body.decode("utf-8")
        self.assertNotIn(API_KEY, status_blob)
        self.assertNotIn(SCAMALYTICS_USERNAME, status_blob)
        self.assertNotIn("token", status_blob.lower())

        # The leak endpoint has an explicit allow-list.  A credential-shaped
        # input is ignored and must not appear in its JSON response.  The
        # basic lookup is patched so this request cannot reach ip-api.
        with mock.patch.object(web, "LEAK_BASIC_LOOKUP",
                               lambda _ip: {"country_code": "US"}):
            status, body = self.post_authorized(
                "/api/leak/evaluate",
                {
                    "candidates": [{"type": "host", "address": "192.168.1.2"}],
                    "exit_ipv4": "198.51.100.1",
                    "ipqs_key": API_KEY,
                    "scamalytics_username": SCAMALYTICS_USERNAME,
                },
            )
        self.assertEqual(status, 200)
        response_blob = body.decode("utf-8")
        self.assertNotIn(API_KEY, response_blob)
        self.assertNotIn(SCAMALYTICS_USERNAME, response_blob)

    def test_runtime_error_and_state_log_are_redacted(self):
        with web._IP_INTEL_SETTINGS_LOCK:
            web._IP_INTEL_OVERRIDES.update({
                "ipinfo_token": API_KEY,
                "ipqs_key": API_KEY,
                "scamalytics_username": SCAMALYTICS_USERNAME,
                "scamalytics_key": API_KEY,
            })
        with mock.patch.object(web, "sync_db"), \
                mock.patch.object(
                    web.subprocess,
                    "Popen",
                    side_effect=RuntimeError(
                        "provider key=%s username=%s" %
                        (API_KEY, SCAMALYTICS_USERNAME)
                    ),
                ):
            web.run_benchmark({})

        with web.STATE_LOCK:
            state_blob = json.dumps(web.STATE, ensure_ascii=False, default=str)
        self.assertNotIn(API_KEY, state_blob)
        self.assertNotIn(SCAMALYTICS_USERNAME, state_blob)
        self.assertIn("[REDACTED]", state_blob)

        status, body = self.request("GET", "/api/run/status")
        self.assertEqual(status, 200)
        response_blob = body.decode("utf-8")
        self.assertNotIn(API_KEY, response_blob)
        self.assertNotIn(SCAMALYTICS_USERNAME, response_blob)


if __name__ == "__main__":
    unittest.main()
