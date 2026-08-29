# -*- coding: utf-8 -*-
import json
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import speedbench_ip_intel as intel
from speedbench_ip_intel import IpInfoProvider, ProviderConfig, load_provider_config


class FakeTransport:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status
        self.urls = []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        return self.status, json.dumps(self.body)


class IpInfoProviderTest(unittest.TestCase):
    def test_max_response_is_normalized(self):
        transport = FakeTransport({
            "ip": "203.0.113.8",
            "asn": {"asn": "AS64500", "name": "Example ISP", "type": "isp"},
            "geo": {"country_code": "US"},
            "company": {"name": "Example ISP"},
            "anonymous": {
                "is_proxy": False,
                "is_vpn": False,
                "is_tor": False,
                "is_relay": False,
                "is_res_proxy": True,
            },
            "is_hosting": False,
            "is_mobile": False,
        })
        result = IpInfoProvider("token-sentinel", transport=transport).query("203.0.113.8")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.normalized["asn"], "AS64500")
        self.assertEqual(result.normalized["country_code"], "US")
        self.assertFalse(result.normalized["hosting"])
        self.assertTrue(result.normalized["residential_proxy"])
        self.assertNotIn("token-sentinel", json.dumps(result.to_dict()))
        self.assertEqual(len(transport.urls), 1)
        self.assertIn("api.ipinfo.io/lookup/203.0.113.8", transport.urls[0])

    def test_missing_optional_tier_fields_stay_none(self):
        result = IpInfoProvider(
            "token", transport=FakeTransport({"ip": "203.0.113.9"})
        ).query("203.0.113.9")
        self.assertEqual(result.status, "ok")
        for key in ("hosting", "proxy", "vpn", "tor", "mobile", "residential_proxy"):
            self.assertIsNone(result.normalized[key])

    def test_asn_owner_fills_isp_and_organization_when_company_missing(self):
        result = IpInfoProvider(
            "token", transport=FakeTransport({
                "ip": "203.0.113.9",
                "asn": {"asn": "AS64509", "name": "Example Consumer ISP", "type": "isp"},
            })
        ).query("203.0.113.9")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.normalized["isp"], "Example Consumer ISP")
        self.assertEqual(result.normalized["organization"], "Example Consumer ISP")
        # The ASN owner is metadata only; no residential inference occurs here.
        self.assertNotIn("residential", result.normalized)

    def test_missing_token_does_not_call_transport(self):
        transport = FakeTransport({})
        result = IpInfoProvider(None, transport=transport).query("203.0.113.9")
        self.assertEqual(result.status, "key_missing")
        self.assertEqual(transport.urls, [])

    def test_timeout_is_sanitized(self):
        def timeout(url, **kwargs):
            raise TimeoutError("https://api.ipinfo.io/?token=token-sentinel")

        result = IpInfoProvider("token-sentinel", transport=timeout).query("203.0.113.9")
        self.assertEqual(result.status, "timeout")
        blob = json.dumps(result.to_dict())
        self.assertNotIn("token-sentinel", blob)
        self.assertNotIn("api.ipinfo.io", blob)

    def test_long_error_is_sanitized_before_truncation(self):
        secret = "super-secret-token-1234567890"

        def failure(url, **kwargs):
            raise RuntimeError("x" * 170 + secret + "y" * 200)

        result = IpInfoProvider(secret, transport=failure).query("203.0.113.9")
        self.assertEqual(result.status, "error")
        self.assertNotIn(secret, result.error or "")
        # Regression guard: the old order could leave a prefix of a long key
        # at the 180-character truncation boundary.
        self.assertNotIn(secret[:10], result.error or "")

    def test_default_transport_disables_environment_proxy(self):
        class Response:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def getcode(self):
                return 200

            def read(self):
                return b'{"ip":"203.0.113.9"}'

        opener = mock.Mock()
        opener.open.return_value = Response()
        with mock.patch.object(intel.urllib.request, "build_opener", return_value=opener) as build, \
                mock.patch.object(intel.urllib.request, "getproxies",
                                  side_effect=AssertionError("environment proxy read")):
            response = intel._urllib_transport("https://example.invalid/lookup", timeout=0.1)
        self.assertEqual(response.status_code, 200)
        handlers = build.call_args.args
        self.assertTrue(any(
            isinstance(handler, urllib.request.ProxyHandler) and handler.proxies == {}
            for handler in handlers
        ))
        self.assertTrue(any(handler is intel._NoRedirectHandler for handler in handlers))

    def test_environment_configuration(self):
        cfg = load_provider_config({
            "SPEEDBENCH_IPINFO_TOKEN": "i",
            "SPEEDBENCH_IPQS_KEY": "q",
            "SPEEDBENCH_SCAMALYTICS_USERNAME": "u",
            "SPEEDBENCH_SCAMALYTICS_KEY": "s",
            "SPEEDBENCH_SCAMALYTICS_REGION": "EU",
        })
        self.assertEqual(cfg, ProviderConfig("i", "q", "u", "s", "eu"))


if __name__ == "__main__":
    unittest.main()
