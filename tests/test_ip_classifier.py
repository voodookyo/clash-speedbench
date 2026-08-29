# -*- coding: utf-8 -*-
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from speedbench_ip_intel import (
    IpIntelligence,
    IpClassification,
    ProviderResult,
    aggregate_ip_intelligence,
    classify_ip,
)


class ClassifierTest(unittest.TestCase):
    def test_residential_requires_more_than_hosting_false(self):
        only_negative = classify_ip({"ipinfo": {"hosting": False, "proxy": False}})
        self.assertEqual(only_negative.category, "unknown")
        self.assertNotEqual(only_negative.category, "residential")

    def test_residential_high_confidence_case(self):
        result = classify_ip({
            "ipqs": {"connection_type": "Residential"},
            "ipinfo": {"hosting": False},
            "scamalytics": {"datacenter": False},
        })
        self.assertEqual(result.category, "residential")
        self.assertGreaterEqual(result.confidence, 80)
        self.assertIn("IPQS Residential", result.evidence)

    def test_residential_proxy_explicit_and_inferred(self):
        explicit = classify_ip({
            "ipinfo": {"residential_proxy": True, "hosting": False},
            "ipqs": {"connection_type": "Residential"},
        })
        self.assertEqual(explicit.category, "residential_proxy")
        inferred = classify_ip({
            "ipqs": {"connection_type": "Residential", "proxy": True},
            "ipinfo": {"hosting": False},
        })
        self.assertEqual(inferred.category, "residential_proxy")
        self.assertTrue(any("inferred" in item for item in inferred.evidence))

    def test_datacenter_multi_source(self):
        result = classify_ip({
            "ipinfo": {"hosting": True},
            "ipqs": {"connection_type": "Data Center"},
            "scamalytics": {"datacenter": True},
        })
        self.assertEqual(result.category, "datacenter")
        self.assertGreaterEqual(result.confidence, 85)

    def test_strong_residential_datacenter_conflict_is_unknown(self):
        result = classify_ip({
            "ipqs": {"connection_type": "Residential"},
            "ipinfo": {"hosting": True},
            "scamalytics": {"datacenter": True},
        })
        self.assertEqual(result.category, "unknown")
        self.assertLess(result.confidence, 50)
        self.assertTrue(result.conflicts)
        blob = " ".join(result.conflicts)
        self.assertIn("Residential", blob)
        self.assertIn("Datacenter", blob)

    def test_categories(self):
        cases = [
            ({"ipqs": {"connection_type": "Corporate"}}, "corporate"),
            ({"ipqs": {"connection_type": "Mobile"}}, "mobile"),
            ({"ipqs": {"vpn": True}}, "vpn_proxy"),
        ]
        for sources, category in cases:
            with self.subTest(category=category):
                self.assertEqual(classify_ip(sources).category, category)

    def test_provider_result_inputs_are_supported(self):
        result = classify_ip([
            ProviderResult("ipqs", "203.0.113.13", "ok",
                           normalized={"connection_type": "Residential"}),
            ProviderResult("ipinfo", "203.0.113.13", "ok",
                           normalized={"hosting": False}),
            ProviderResult("scamalytics", "203.0.113.13", "ok",
                           normalized={"datacenter": False}),
        ])
        self.assertEqual(result.category, "residential")

    def test_aggregate_model_is_serializable(self):
        providers = {
            "ipqs": ProviderResult("ipqs", "203.0.113.13", "ok",
                                    normalized={"connection_type": "Residential",
                                                "fraud_score": 8}),
            "ipinfo": ProviderResult("ipinfo", "203.0.113.13", "ok",
                                      normalized={"hosting": False}),
        }
        result = aggregate_ip_intelligence("203.0.113.13", providers)
        self.assertIsInstance(result, IpIntelligence)
        self.assertEqual(result.classification.category, "residential")
        self.assertEqual(result.ipqs_fraud_score, 8)
        json.dumps(result.to_dict())


if __name__ == "__main__":
    unittest.main()
