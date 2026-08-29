# -*- coding: utf-8 -*-
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from speedbench_ip_intel import IpIntelCache, ScamalyticsProvider


class FakeTransport:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status
        self.urls = []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        return self.status, self.body


class ScamalyticsProviderTest(unittest.TestCase):
    def test_v3_response_is_normalized(self):
        transport = FakeTransport({
            "status": "success",
            "scamalytics": {
                "ip": "203.0.113.11",
                "scamalytics_score": 12,
                "scamalytics_risk": "low",
                "scamalytics_isp_score": 8,
                "scamalytics_isp_risk": "low",
                "scamalytics_proxy": {
                    "is_datacenter": False,
                    "is_vpn": False,
                    "is_tor": False,
                    "is_server": False,
                    "is_proxy": False,
                    "is_blacklisted_external": False,
                },
            },
        })
        result = ScamalyticsProvider(
            username="user-sentinel", key="key-sentinel", region="eu",
            transport=transport,
        ).query("203.0.113.11")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.normalized["scamalytics_score"], 12)
        self.assertFalse(result.normalized["datacenter"])
        self.assertFalse(result.normalized["vpn"])
        self.assertFalse(result.normalized["tor"])
        self.assertFalse(result.normalized["server"])
        self.assertFalse(result.normalized["proxy"])
        self.assertFalse(result.normalized["blacklisted"])
        self.assertIn("api12.scamalytics.com", transport.urls[0])
        self.assertNotIn("key-sentinel", json.dumps(result.to_dict()))
        self.assertNotIn("user-sentinel", json.dumps(result.to_dict()))

    def test_region_is_required_and_never_guessed(self):
        transport = FakeTransport({})
        result = ScamalyticsProvider(
            username="user", key="key", transport=transport
        ).query("203.0.113.11")
        self.assertEqual(result.status, "configuration_incomplete")
        self.assertEqual(transport.urls, [])

    def test_invalid_region_is_configuration_error(self):
        result = ScamalyticsProvider(
            username="user", key="key", region="asia", transport=FakeTransport({})
        ).query("203.0.113.11")
        self.assertEqual(result.status, "configuration_incomplete")

    def test_quota_http_status(self):
        result = ScamalyticsProvider(
            username="user", key="key", region="us",
            transport=FakeTransport({}, status=403),
        ).query("203.0.113.11")
        self.assertEqual(result.status, "quota_unavailable")

    def test_premium_placeholder_is_not_true(self):
        result = ScamalyticsProvider(
            username="user", key="key", region="us",
            transport=FakeTransport({
                "scamalytics": {
                    "scamalytics_score": 15,
                    "scamalytics_proxy": {
                        "is_datacenter": "PREMIUM FIELD - upgrade to view",
                        "is_blacklisted_external": "PREMIUM FIELD - upgrade to view",
                    },
                },
            }),
        ).query("203.0.113.11")
        self.assertEqual(result.status, "ok")
        self.assertIsNone(result.normalized["datacenter"])
        self.assertIsNone(result.normalized["blacklisted"])

    def test_nested_error_status_and_message_are_not_cached_as_success(self):
        cases = [
            ({"status": "error", "message": "credits quota exhausted"}, "quota_unavailable"),
            ({"status": "error", "error": "invalid account"}, "error"),
        ]
        for nested, expected_status in cases:
            with self.subTest(expected_status=expected_status), tempfile.TemporaryDirectory() as td:
                provider = ScamalyticsProvider(
                    username="user", key="key", region="eu",
                    transport=FakeTransport({"scamalytics": nested}),
                )
                result = provider.query("203.0.113.11")
                self.assertEqual(result.status, expected_status)
                cache = IpIntelCache(Path(td) / "intel.db")
                cached_result = cache.get_or_query(provider, "203.0.113.11")
                self.assertEqual(cached_result.status, expected_status)
                self.assertIsNone(cache.get("scamalytics", "203.0.113.11"))

    def test_nested_v3_proxy_signals_are_kept_independent(self):
        result = ScamalyticsProvider(
            username="user", key="key", region="us",
            transport=FakeTransport({
                "status": "success",
                "scamalytics": {
                    "scamalytics_score": 91,
                    "scamalytics_risk": "very high",
                    "scamalytics_proxy": {
                        "is_datacenter": True,
                        "is_vpn": True,
                        "is_tor": True,
                        "is_server": True,
                        "is_proxy": True,
                        "is_blacklisted_external": True,
                    },
                },
            }),
        ).query("203.0.113.11")
        self.assertEqual(result.status, "ok")
        normalized = result.normalized
        self.assertEqual(normalized["scamalytics_score"], 91)
        self.assertEqual(normalized["scamalytics_risk"], "very high")
        self.assertTrue(normalized["datacenter"])
        self.assertTrue(normalized["vpn"])
        self.assertTrue(normalized["tor"])
        self.assertTrue(normalized["server"])
        self.assertTrue(normalized["proxy"])
        self.assertTrue(normalized["blacklisted"])


if __name__ == "__main__":
    unittest.main()
