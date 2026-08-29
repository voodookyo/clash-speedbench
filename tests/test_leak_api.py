# -*- coding: utf-8 -*-
"""WebRTC leak evaluator and localhost API regression tests.

All third-party/basic lookups are injected fixtures.  These tests must never
contact ipify, ip-api, BrowserLeaks, DNSLeakTest or paid reputation APIs.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import speedbench_leak as leak
import speedbench_web as web
from tests.web_server_case import WebServerCase


class CandidateEvaluatorTest(unittest.TestCase):
    def test_private_and_mdns_are_not_public_leaks(self):
        result = leak.evaluate_webrtc([
            {"type": "host", "address": "192.168.1.5"},
            {"type": "host", "address": "abc123.local"},
        ], exit_ipv4="1.1.1.1")
        self.assertEqual(result.status, "unknown")
        self.assertFalse(result.warnings)
        self.assertFalse(result.public_candidates)

    def test_a_line_fallback_and_public_mismatch(self):
        result = leak.evaluate_webrtc([
            {"candidate": "candidate:1 1 UDP 2122260223 8.8.8.8 4567 typ srflx"},
        ], exit_ipv4="1.1.1.1")
        self.assertEqual(result.status, "warning")
        self.assertTrue(any("不一致" in item for item in result.warnings))
        self.assertEqual(result.public_candidates[0]["type"], "srflx")

    def test_unproxied_ipv6_is_warning(self):
        result = leak.evaluate_webrtc([
            {"type": "srflx", "address": "2001:4860:4860::2"},
        ], exit_ipv4="1.1.1.1", exit_ipv6=None)
        self.assertEqual(result.status, "warning")
        self.assertTrue(any("IPv6" in item for item in result.warnings))

    def test_china_unicom_basic_lookup_is_warning(self):
        lookup = lambda ip: {"country_code": "CN", "isp": "China Unicom"}
        result = leak.evaluate_webrtc([
            {"type": "srflx", "address": "8.8.8.8"},
        ], exit_ipv4="8.8.8.8", basic_lookup=lookup)
        self.assertEqual(result.status, "warning")
        self.assertTrue(any("中国" in item for item in result.warnings))

    def test_policy_blocked_or_stun_failure_is_unknown(self):
        result = leak.evaluate_webrtc([], exit_ipv4="1.1.1.1",
                                      policy_blocked=True,
                                      collection_error="stun_failed")
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.status_text, "无法确认")

    def test_private_and_documentation_addresses_are_not_global(self):
        for value in ("10.0.0.1", "127.0.0.1", "169.254.1.1",
                      "192.0.2.1", "2001:db8::1", "abc.local"):
            self.assertFalse(leak.is_public_address(value), value)


class LeakApiTest(WebServerCase):
    def post_eval(self, payload, path="/api/leak/evaluate", headers=None):
        h = {"X-SpeedBench-Token": web.WEB_TOKEN}
        h.update(headers or {})
        status, raw = self.post_authorized(path, payload, headers=headers)
        return status, json.loads(raw.decode("utf-8"))

    def test_new_post_routes_keep_token_host_origin_gate(self):
        for path in ("/api/leak/evaluate", "/api/leak/audit", "/api/ip-intel/settings"):
            status, _ = self.post_json(path, {})
            self.assertEqual(status, 403)
            status, _ = self.post_authorized(path, {}, headers={"Origin": "http://evil.example"})
            self.assertEqual(status, 403)

    def test_evaluate_endpoint_uses_injected_basic_lookup_only(self):
        calls = []
        with mock.patch.object(web, "LEAK_BASIC_LOOKUP",
                               lambda ip: calls.append(ip) or {"country_code": "US"}):
            status, data = self.post_eval({
                "candidates": [{"type": "srflx", "address": "8.8.8.8"}],
                "exit_ipv4": "1.1.1.1",
                # A paid provider payload is not an accepted input contract.
                "ipqs_key": "secret-sentinel",
            })
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "warning")
        self.assertEqual(calls, ["8.8.8.8"])
        self.assertNotIn("secret-sentinel", json.dumps(data))

    def test_save_endpoint_is_safe_when_db_adapter_is_missing(self):
        with mock.patch.object(web.speedbench_db, "insert_leak_audit", None), \
                mock.patch.object(web, "LEAK_BASIC_LOOKUP",
                                  lambda _ip: {"country_code": "US"}):
            status, data = self.post_eval({
                "candidates": [{"type": "srflx", "address": "8.8.8.8"}],
                "exit_ipv4": "1.1.1.1",
            }, "/api/leak/audit")
        self.assertEqual(status, 200)
        self.assertFalse(data["persistence"]["available"])

    def test_history_endpoint_is_safe_when_db_adapter_is_missing(self):
        with mock.patch.object(web.speedbench_db, "leak_audits", None):
            status, raw = self.request("GET", "/api/leak/audits")
        self.assertEqual(status, 200)
        data = json.loads(raw.decode("utf-8"))
        self.assertFalse(data["available"])
        self.assertEqual(data["audits"], [])

    def test_web_provider_status_reports_ip_api_disabled_from_environment(self):
        observed = {"results": [{
            "intel_v4": {"provider_status": {"ip-api": "cache_hit"}}
        }]}
        with mock.patch.dict(os.environ, {"SPEEDBENCH_DISABLE_IP_API": "1"}, clear=False), \
                mock.patch.object(web, "latest_record", return_value=observed):
            status, raw = self.request("GET", "/api/ip-intel/status")

        self.assertEqual(status, 200)
        data = json.loads(raw.decode("utf-8"))
        self.assertEqual(data["providers"]["ip-api"]["status"], "disabled")
        self.assertFalse(data["providers"]["ip-api"]["configured"])

    def test_reenabled_ip_api_ignores_stale_disabled_history(self):
        observed = {"results": [{
            "intel_v4": {"provider_status": {"ip-api": "disabled"}}
        }]}
        with mock.patch.dict(os.environ, {"SPEEDBENCH_DISABLE_IP_API": ""}, clear=False), \
                mock.patch.object(web, "latest_record", return_value=observed):
            status, raw = self.request("GET", "/api/ip-intel/status")

        self.assertEqual(status, 200)
        data = json.loads(raw.decode("utf-8"))
        self.assertEqual(data["providers"]["ip-api"]["status"], "ok")
        self.assertTrue(data["providers"]["ip-api"]["configured"])

    def test_disabled_ip_api_is_not_used_by_basic_leak_lookup(self):
        transport = mock.Mock(return_value=(200, {
            "status": "success", "query": "203.0.113.25",
        }))
        with mock.patch.dict(os.environ, {"SPEEDBENCH_DISABLE_IP_API": "1"}, clear=False), \
                mock.patch.object(web.speedbench_ip_intel, "_urllib_transport", transport):
            self.assertEqual(web._basic_ip_lookup("203.0.113.25"), {})
        transport.assert_not_called()


class IpIntelSettingsTest(WebServerCase):
    def setUp(self):
        super().setUp()
        self.old = dict(web._IP_INTEL_OVERRIDES)
        web._IP_INTEL_OVERRIDES.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        web._IP_INTEL_OVERRIDES.clear()
        web._IP_INTEL_OVERRIDES.update(self.old)

    def test_settings_are_memory_only_and_never_returned(self):
        values = {
            "ipinfo_token": "token-secret-sentinel",
            "ipqs_key": "ipqs-secret-sentinel",
            "scamalytics_username": "user-secret-sentinel",
            "scamalytics_key": "scam-secret-sentinel",
            "scamalytics_region": "eu",
        }
        status, raw = self.post_authorized("/api/ip-intel/settings", values)
        self.assertEqual(status, 200)
        self.assertNotIn("sentinel", raw.decode("utf-8"))
        status, raw = self.request("GET", "/api/ip-intel/status")
        self.assertEqual(status, 200)
        body = raw.decode("utf-8")
        self.assertNotIn("sentinel", body)
        data = json.loads(body)
        self.assertTrue(data["providers"]["ipinfo"]["configured"])
        self.assertTrue(data["providers"]["scamalytics"]["configured"])
        env = web._provider_env_snapshot()
        self.assertEqual(env["SPEEDBENCH_IPQS_KEY"], values["ipqs_key"])

    def test_region_and_length_validation_do_not_echo_secret(self):
        status, raw = self.post_authorized("/api/ip-intel/settings", {
            "scamalytics_region": "asia",
            "ipqs_key": "secret-sentinel",
        })
        self.assertEqual(status, 400)
        self.assertNotIn("secret-sentinel", raw.decode("utf-8"))
        status, _ = self.post_authorized("/api/ip-intel/settings", {
            "ipqs_key": "x" * 513,
        })
        self.assertEqual(status, 400)

    def test_empty_values_clear_overrides(self):
        self.post_authorized("/api/ip-intel/settings", {"ipqs_key": "secret-sentinel"})
        self.post_authorized("/api/ip-intel/settings", {"ipqs_key": ""})
        status, raw = self.request("GET", "/api/ip-intel/status")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw.decode("utf-8"))["providers"]["ipqs"]["configured"])


if __name__ == "__main__":
    unittest.main()
