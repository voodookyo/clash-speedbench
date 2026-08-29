# -*- coding: utf-8 -*-
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from speedbench_ip_intel import (
    IpApiProvider,
    IpIntelCache,
    ProviderResult,
    ProviderConfig,
    load_provider_config,
    make_default_providers,
    provider_status_snapshot,
)


class FakeTransport:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return 200, json.dumps(self.body)


class IpApiProviderTest(unittest.TestCase):
    def test_enabled_by_default_for_no_key_compatibility(self):
        transport = FakeTransport({
            "status": "success",
            "query": "203.0.113.20",
            "countryCode": "US",
        })
        with mock.patch.dict(os.environ, {"SPEEDBENCH_DISABLE_IP_API": ""}, clear=False):
            provider = IpApiProvider(transport=transport)
            result = provider.query("203.0.113.20")

        self.assertEqual(result.status, "ok")
        self.assertEqual(len(transport.calls), 1)

    def test_disabled_provider_reports_disabled_without_transport_call(self):
        transport = FakeTransport({"status": "success"})
        provider = IpApiProvider(enabled=False, transport=transport)

        result = provider.query("203.0.113.21")

        self.assertEqual(result.status, "disabled")
        self.assertEqual(provider.configured_status(), "disabled")
        self.assertEqual(provider_status_snapshot([provider]), {"ip-api": "disabled"})
        self.assertEqual(transport.calls, [])

    def test_default_constructor_honors_environment_opt_out(self):
        transport = FakeTransport({"status": "success"})
        with mock.patch.dict(os.environ, {"SPEEDBENCH_DISABLE_IP_API": "1"}, clear=False):
            provider = IpApiProvider(transport=transport)
            result = provider.query("203.0.113.23")

        self.assertEqual(result.status, "disabled")
        self.assertEqual(transport.calls, [])

    def test_string_provider_name_opt_out_skips_cache_and_callback(self):
        with tempfile.TemporaryDirectory() as td:
            cache = IpIntelCache(Path(td) / "intel.db")
            cache.put(ProviderResult(
                "ip-api", "203.0.113.24", "ok",
                normalized={"country_code": "US"},
            ))
            callback = mock.Mock(side_effect=AssertionError("disabled callback called"))
            with mock.patch.dict(os.environ, {"SPEEDBENCH_DISABLE_IP_API": "1"}, clear=False):
                result = cache.get_or_query(
                    "ip-api", "203.0.113.24", query=callback
                )
                direct_cache = cache.get("ip-api", "203.0.113.24")

        self.assertEqual(result.status, "disabled")
        self.assertIsNone(direct_cache)
        callback.assert_not_called()

    def test_disabled_provider_does_not_reuse_existing_cache(self):
        with tempfile.TemporaryDirectory() as td:
            cache = IpIntelCache(Path(td) / "intel.db")
            cache.put(ProviderResult(
                "ip-api", "203.0.113.22", "ok",
                normalized={"country_code": "US"},
            ))
            transport = FakeTransport({"status": "success"})
            provider = IpApiProvider(enabled=False, transport=transport)
            callback = mock.Mock(side_effect=AssertionError("disabled query bypassed"))

            result = cache.get_or_query(provider, "203.0.113.22", query=callback)

        self.assertEqual(result.status, "disabled")
        self.assertNotEqual(result.status, "cache_hit")
        self.assertEqual(transport.calls, [])
        callback.assert_not_called()

    def test_disable_environment_variable_is_explicit_and_visible_in_default_providers(self):
        config = load_provider_config({"SPEEDBENCH_DISABLE_IP_API": "1"})

        self.assertFalse(config.ip_api_enabled)
        providers = make_default_providers(config=config, transport=FakeTransport({}))
        statuses = provider_status_snapshot(providers)

        self.assertEqual(statuses["ip-api"], "disabled")
        self.assertEqual([provider.name for provider in providers].count("ip-api"), 1)

    def test_other_disable_values_keep_default_enabled_behavior(self):
        config = load_provider_config({"SPEEDBENCH_DISABLE_IP_API": "0"})
        self.assertTrue(config.ip_api_enabled)
        self.assertEqual(
            load_provider_config({}).ip_api_enabled,
            ProviderConfig().ip_api_enabled,
        )


if __name__ == "__main__":
    unittest.main()
