# -*- coding: utf-8 -*-
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from speedbench_ip_intel import (
    DEFAULT_COOLDOWN_SECONDS,
    IpApiProvider,
    IpIntelCache,
    IpqsProvider,
)


class FakeTransport:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status
        self.urls = []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        return self.status, self.body


class SequenceTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        return self.responses.pop(0)


class IpqsProviderTest(unittest.TestCase):
    def test_documented_fields_are_normalized(self):
        transport = FakeTransport({
            "success": True,
            "ISP": "Example Broadband",
            "organization": "Example Org",
            "ASN": 64501,
            "connection_type": "Residential",
            "proxy": True,
            "vpn": False,
            "tor": False,
            "mobile": False,
            "fraud_score": 71,
            "recent_abuse": True,
            "abuse_velocity": "high",
            "bot_status": False,
        })
        result = IpqsProvider("key-sentinel", transport=transport).query("203.0.113.10")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.normalized["isp"], "Example Broadband")
        self.assertEqual(result.normalized["asn"], "64501")
        self.assertEqual(result.normalized["connection_type"], "Residential")
        self.assertEqual(result.normalized["fraud_score"], 71)
        self.assertTrue(result.normalized["recent_abuse"])
        self.assertIs(result.normalized["bot_status"], False)
        self.assertNotIn("key-sentinel", json.dumps(result.to_dict()))

    def test_missing_key(self):
        transport = FakeTransport({})
        result = IpqsProvider(transport=transport).query("203.0.113.10")
        self.assertEqual(result.status, "key_missing")
        self.assertFalse(transport.urls)

    def test_quota_message(self):
        result = IpqsProvider(
            "key", transport=FakeTransport({"success": False, "message": "quota exceeded"})
        ).query("203.0.113.10")
        self.assertEqual(result.status, "quota_unavailable")

    def test_rate_limit_http_status(self):
        result = IpqsProvider("key", transport=FakeTransport({}, status=429)).query("203.0.113.10")
        self.assertEqual(result.status, "rate_limited")

    def test_plain_text_rate_limit_is_not_invalid_json_and_not_cached(self):
        provider = IpqsProvider(
            "key", transport=FakeTransport("rate limit exceeded", status=429)
        )
        result = provider.query("203.0.113.10")
        self.assertEqual(result.status, "rate_limited")
        with tempfile.TemporaryDirectory() as td:
            cache = IpIntelCache(Path(td) / "intel.db")
            result = cache.get_or_query(provider, "203.0.113.10")
            self.assertEqual(result.status, "rate_limited")
            self.assertIsNone(cache.get("ipqs", "203.0.113.10"))

    def test_provider_cooldown_blocks_other_ips_until_retry_after(self):
        clock = [1000.0]
        transport = SequenceTransport([
            (429, "rate limited", {"retry-after": "9"}),
            (200, {"success": True, "fraud_score": 2}, {}),
        ])
        provider = IpqsProvider("key", transport=transport, clock=lambda: clock[0])
        self.assertEqual(provider.query("203.0.113.10").status, "rate_limited")
        self.assertEqual(provider.query("203.0.113.11").status, "rate_limited")
        self.assertEqual(len(transport.urls), 1)
        clock[0] = 1009.1
        self.assertEqual(provider.query("203.0.113.11").status, "ok")
        self.assertEqual(len(transport.urls), 2)

    def test_missing_retry_headers_use_short_default_cooldown(self):
        clock = [2000.0]
        transport = SequenceTransport([
            (429, "rate limited", {}),
            (200, {"success": True, "fraud_score": 2}, {}),
        ])
        provider = IpqsProvider("key", transport=transport, clock=lambda: clock[0])
        self.assertEqual(provider.query("203.0.113.12").status, "rate_limited")
        self.assertAlmostEqual(provider.cooldown_remaining(), DEFAULT_COOLDOWN_SECONDS)
        self.assertEqual(provider.query("203.0.113.13").status, "rate_limited")
        self.assertEqual(len(transport.urls), 1)

    def test_ip_api_x_rl_zero_and_x_ttl_cooldown_are_case_insensitive(self):
        # Verify the enabled provider's header handling independently of the
        # outer-suite SPEEDBENCH_DISABLE_IP_API setting.
        with mock.patch.dict(os.environ, {"SPEEDBENCH_DISABLE_IP_API": ""}, clear=False):
            clock = [3000.0]
            transport = SequenceTransport([
                (200, {
                    "status": "success", "query": "203.0.113.14",
                    "countryCode": "US", "isp": "Example ISP",
                }, {"x-rL": "0", "x-tTl": "4"}),
                (200, {
                    "status": "success", "query": "203.0.113.15",
                    "countryCode": "US", "isp": "Example ISP",
                }, {}),
            ])
            provider = IpApiProvider(transport=transport, clock=lambda: clock[0])
            self.assertEqual(provider.query("203.0.113.14").status, "ok")
            self.assertEqual(provider.query("203.0.113.15").status, "rate_limited")
            self.assertEqual(len(transport.urls), 1)
            clock[0] = 3004.1
            self.assertEqual(provider.query("203.0.113.15").status, "ok")
            self.assertEqual(len(transport.urls), 2)

    def test_invalid_json(self):
        result = IpqsProvider("key", transport=FakeTransport("not-json")).query("203.0.113.10")
        self.assertEqual(result.status, "invalid_response")


if __name__ == "__main__":
    unittest.main()
