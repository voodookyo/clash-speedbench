# -*- coding: utf-8 -*-
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from speedbench_ip_intel import IpApiProvider, IpIntelCache, ProviderResult


class CacheTest(unittest.TestCase):
    def test_ttl_and_expiry(self):
        now = [1000.0]
        with tempfile.TemporaryDirectory() as td:
            cache = IpIntelCache(Path(td) / "intel.db", basic_ttl=70,
                                 risk_ttl=10, clock=lambda: now[0])
            result = ProviderResult("ip-api", "203.0.113.20", "ok",
                                    raw={"country": "US"}, normalized={"country": "US"})
            cache.put(result)
            hit = cache.get("ip-api", "203.0.113.20")
            self.assertIsNotNone(hit)
            self.assertEqual(hit.status, "cache_hit")
            self.assertEqual(hit.expires_at, 1070.0)
            now[0] = 1070.0
            self.assertIsNone(cache.get("ip-api", "203.0.113.20"))

    def test_risk_ttl_is_shorter(self):
        now = [1000.0]
        with tempfile.TemporaryDirectory() as td:
            cache = IpIntelCache(Path(td) / "intel.db", basic_ttl=70,
                                 risk_ttl=10, clock=lambda: now[0])
            cache.put(ProviderResult("ipqs", "203.0.113.21", "ok",
                                     normalized={"fraud_score": 2}))
            self.assertEqual(cache.get("ipqs", "203.0.113.21").expires_at, 1010.0)

    def test_same_provider_ip_single_flight(self):
        with tempfile.TemporaryDirectory() as td:
            cache = IpIntelCache(Path(td) / "intel.db")
            count = [0]
            lock = threading.Lock()

            def query(ip):
                with lock:
                    count[0] += 1
                time.sleep(0.05)
                return ProviderResult("ipqs", ip, "ok",
                                      normalized={"fraud_score": 4})

            out = []
            threads = [threading.Thread(
                target=lambda: out.append(cache.get_or_query("ipqs", "203.0.113.22", query))
            ) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(count[0], 1)
            self.assertEqual(len(out), 8)
            self.assertTrue(all(item.status == "ok" for item in out))

    def test_query_many_deduplicates_duplicate_provider_names(self):
        with tempfile.TemporaryDirectory() as td:
            cache = IpIntelCache(Path(td) / "intel.db")
            calls = []

            class FakeProvider:
                name = "ip-api"

                def query(self, ip):
                    calls.append(ip)
                    return ProviderResult(self.name, ip, "ok", normalized={"country": "US"})

            out = cache.query_many("203.0.113.23", [FakeProvider(), FakeProvider()])
            self.assertEqual(list(out), ["ip-api"])
            self.assertEqual(calls, ["203.0.113.23"])

    def test_cached_json_is_sanitized(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "intel.db"
            cache = IpIntelCache(path, secrets=("secret-sentinel",))
            cache.put(ProviderResult(
                "ipqs", "203.0.113.24", "ok",
                raw={"api_key": "secret-sentinel", "message": "secret-sentinel"},
                normalized={"token": "secret-sentinel", "fraud_score": 1},
            ))
            blob = path.read_bytes().decode("utf-8", "replace")
            self.assertNotIn("secret-sentinel", blob)
            hit = cache.get("ipqs", "203.0.113.24")
            self.assertNotIn("secret-sentinel", json.dumps(hit.to_dict()))


if __name__ == "__main__":
    unittest.main()
