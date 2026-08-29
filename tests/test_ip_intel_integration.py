# -*- coding: utf-8 -*-
"""Main-process Intelligence enrichment integration (all transports mocked)."""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clash_speedbench as csb
from speedbench_ip_intel import (
    IpIntelligence,
    ProviderResult,
    aggregate_ip_intelligence,
)


def result(name, ipv4=None, ipv6=None):
    return csb.Result(
        name=name, provider="p", proto="ss", latency_ms=80,
        speeds_mbps=[100.0], median_mbps=100.0, best_mbps=100.0,
        status="ok", exit_ipv4=ipv4, exit_ipv6=ipv6,
    )


class FakeProvider:
    name = "ipqs"
    ttl_seconds = 86400

    def __init__(self):
        self.calls = []

    def query(self, ip):
        self.calls.append(ip)
        return ProviderResult(
            self.name, ip, "ok",
            normalized={
                "country": "US", "asn": "AS64500", "connection_type": "Residential",
                "fraud_score": 4, "proxy": False, "vpn": False, "tor": False,
                "recent_abuse": False,
            },
        )


class IntelligenceIntegrationTest(unittest.TestCase):
    def test_same_exit_ip_is_submitted_once_and_cached(self):
        with tempfile.TemporaryDirectory() as td:
            fake = FakeProvider()
            args = SimpleNamespace(
                history=str(Path(td) / "history.jsonl"), no_ip=False,
                ip_timeout=1, intel_workers=2,
            )
            first = result("A", ipv4="203.0.113.40")
            second = result("B", ipv4="203.0.113.40")
            with mock.patch.object(csb, "make_default_providers", return_value=[fake]):
                enricher = csb.start_intelligence_enrichment([first, second], args)
                self.assertIsNotNone(enricher)
                enricher.submit_result(first)
                enricher.submit_result(second)
                csb.finish_intelligence_enrichment(enricher, [first, second])
            self.assertEqual(fake.calls, ["203.0.113.40"])
            self.assertEqual(first.intel_v4.ip, "203.0.113.40")
            self.assertEqual(second.intel_v4.ip_quality_score, 100.0)
            self.assertEqual(first.ip_grade, "S")
            db = sqlite3.connect(Path(td) / "history.db")
            try:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM ip_intel_cache").fetchone()[0], 1
                )
            finally:
                db.close()

    def test_worse_dual_stack_quality_and_inconsistency_are_exposed(self):
        v4 = aggregate_ip_intelligence("198.51.100.41", {
            "ipqs": ProviderResult(
                "ipqs", "198.51.100.41", "ok",
                normalized={"country": "US", "connection_type": "Residential",
                            "fraud_score": 5},
            )
        })
        v6 = aggregate_ip_intelligence("2001:db8::41", {
            "ipqs": ProviderResult(
                "ipqs", "2001:db8::41", "ok",
                normalized={"country": "CN", "connection_type": "Data Center",
                            "fraud_score": 96},
            )
        })
        r = result("dual", ipv4="198.51.100.41", ipv6="2001:db8::41")
        csb._apply_intelligence(r, {v4.ip: v4, v6.ip: v6})
        self.assertEqual(r.ip_quality_score, 32.0)
        self.assertEqual(r.ip_grade, "D")
        self.assertTrue(r.dual_stack_inconsistent)
        self.assertIn("双栈出口不一致", csb.make_tags(r))

    def test_legacy_ip_identity_is_resolved_before_intel_attachment(self):
        ip = "198.51.100.44"
        intel = aggregate_ip_intelligence(ip, {
            "ipqs": ProviderResult(
                "ipqs", ip, "ok", normalized={"fraud_score": 4}
            )
        })
        r = result("legacy", ipv4=None)
        r.ip = csb.IpInfo(exit_ip=ip, ok=True)

        csb._apply_intelligence(r, {ip: intel})

        self.assertEqual(r.exit_ipv4, ip)
        self.assertIs(r.intel_v4, intel)

    def test_legacy_ip_api_fallback_supplies_ipv4(self):
        data = {"status": "success", "query": "203.0.113.42"}
        with mock.patch.dict(os.environ, {"SPEEDBENCH_DISABLE_IP_API": ""}, clear=False), \
                mock.patch.object(csb, "fetch_ip_info", return_value=data), \
                mock.patch.object(csb, "fetch_exit_ip", side_effect=[None, None]):
            ipv4, ipv6, legacy = csb.fetch_exit_ips("http://127.0.0.1:1", 1)
        self.assertEqual(ipv4, "203.0.113.42")
        self.assertIsNone(ipv6)
        self.assertIs(legacy, data)

    def test_disabled_ip_api_skips_legacy_request_but_keeps_dual_stack_ipify(self):
        """Opt-out must remove only the legacy HTTP request, not ipify probes."""
        barrier = threading.Barrier(2)
        ipify_calls = []

        def fake_ipify(_proxy_url, _timeout, ipv6=False):
            ipify_calls.append(ipv6)
            barrier.wait(timeout=2)
            return "2001:db8::45" if ipv6 else "203.0.113.45"

        with mock.patch.dict(os.environ, {"SPEEDBENCH_DISABLE_IP_API": "1"}, clear=False), \
                mock.patch.object(csb, "fetch_exit_ip", side_effect=fake_ipify), \
                mock.patch.object(csb, "fetch_ip_info") as legacy:
            ipv4, ipv6, legacy_payload = csb.fetch_exit_ips(
                "http://127.0.0.1:1", 1
            )

        self.assertEqual(sorted(ipify_calls), [False, True])
        self.assertEqual(ipv4, "203.0.113.45")
        self.assertEqual(ipv6, "2001:db8::45")
        self.assertIsNone(legacy_payload)
        legacy.assert_not_called()

    def test_disabled_ip_api_sink_does_not_invoke_curl(self):
        with mock.patch.dict(os.environ, {"SPEEDBENCH_DISABLE_IP_API": "1"}, clear=False), \
                mock.patch.object(csb.subprocess, "run") as run:
            self.assertIsNone(csb.fetch_ip_info("http://127.0.0.1:1", 1))
        run.assert_not_called()

    def test_build_hosts_skips_disabled_ip_api_domain_but_keeps_ipify(self):
        import speedbench_workers as sbw

        calls = []

        def fake_doh(domain, record_type="A"):
            calls.append((domain, record_type))
            return "2001:db8::46" if record_type == "AAAA" else "192.0.2.46"

        with mock.patch.dict(os.environ, {"SPEEDBENCH_DISABLE_IP_API": "1"}, clear=False), \
                mock.patch.object(sbw, "doh_resolve", side_effect=fake_doh):
            hosts = sbw.build_hosts([{"server": "node.example"}])

        self.assertNotIn("ip-api.com", {domain for domain, _ in calls})
        self.assertNotIn("ip-api.com", hosts)
        self.assertIn("api.ipify.org", hosts)
        self.assertIn("api6.ipify.org", hosts)

    def test_build_hosts_keeps_ip_api_domain_by_default(self):
        import speedbench_workers as sbw

        calls = []

        def fake_doh(domain, record_type="A"):
            calls.append((domain, record_type))
            return "2001:db8::47" if record_type == "AAAA" else "192.0.2.47"

        with mock.patch.dict(os.environ, {"SPEEDBENCH_DISABLE_IP_API": ""}, clear=False), \
                mock.patch.object(sbw, "doh_resolve", side_effect=fake_doh):
            hosts = sbw.build_hosts([{"server": "node.example"}])

        self.assertIn("ip-api.com", {domain for domain, _ in calls})
        self.assertIn("ip-api.com", hosts)

    def test_exit_ip_requests_are_deterministically_overlapped(self):
        """The three independent exit probes must start before any returns.

        A barrier makes this a deterministic concurrency assertion rather than
        a timing-based sleep test: a sequential implementation cannot pass the
        barrier and would return unavailable values instead.
        """
        barrier = threading.Barrier(3)
        lock = threading.Lock()
        active = 0
        peak = 0

        def enter_probe():
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait(timeout=2)
            finally:
                with lock:
                    active -= 1

        def fake_ipify(_proxy_url, _timeout, ipv6=False):
            enter_probe()
            return "2001:db8::44" if ipv6 else "203.0.113.44"

        def fake_legacy(_proxy_url, _timeout):
            enter_probe()
            return {"status": "success", "query": "203.0.113.44"}

        with mock.patch.dict(os.environ, {"SPEEDBENCH_DISABLE_IP_API": ""}, clear=False), \
                mock.patch.object(csb, "fetch_exit_ip", side_effect=fake_ipify), \
                mock.patch.object(csb, "fetch_ip_info", side_effect=fake_legacy):
            ipv4, ipv6, legacy = csb.fetch_exit_ips("http://127.0.0.1:1", 1)

        self.assertEqual(peak, 3)
        self.assertEqual(ipv4, "203.0.113.44")
        self.assertEqual(ipv6, "2001:db8::44")
        self.assertEqual(legacy["query"], ipv4)

    def test_exit_aggregate_rejects_wrong_address_family(self):
        data = {"status": "success", "query": "203.0.113.45"}
        with mock.patch.dict(os.environ, {"SPEEDBENCH_DISABLE_IP_API": ""}, clear=False), \
                mock.patch.object(
                csb, "fetch_exit_ip",
                side_effect=lambda _proxy, _timeout, ipv6=False:
                    "203.0.113.46" if ipv6 else "2001:db8::45"), \
                mock.patch.object(csb, "fetch_ip_info", return_value=data):
            ipv4, ipv6, _legacy = csb.fetch_exit_ips("http://127.0.0.1:1", 1)

        self.assertEqual(ipv4, "203.0.113.45")  # validated ip-api fallback
        self.assertIsNone(ipv6)

    def test_result_history_has_no_provider_raw_or_secret(self):
        sentinel = "secret-sentinel"
        intel = IpIntelligence(
            ip="203.0.113.43", provider_results={
                "ipqs": ProviderResult(
                    "ipqs", "203.0.113.43", "ok",
                    raw={"api_key": sentinel, "response": sentinel},
                    normalized={"fraud_score": 4},
                )
            },
        )
        r = result("safe", ipv4="203.0.113.43")
        r.intel_v4 = intel
        payload = csb.result_to_dict(r)
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(sentinel, blob)
        self.assertNotIn("provider_results", payload["intel_v4"])
        self.assertNotIn("raw", payload["intel_v4"])


if __name__ == "__main__":
    unittest.main()
