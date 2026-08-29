# -*- coding: utf-8 -*-
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from speedbench_ip_intel import (
    aggregate_ip_intelligence,
    compute_ip_grade,
    compute_ip_quality_score,
    ip_quality_grade,
)


class IpGradeTest(unittest.TestCase):
    def test_ip_api_only_is_unknown_not_clean(self):
        self.assertIsNone(compute_ip_quality_score({
            "ip-api": {"hosting": False, "proxy": False, "mobile": False}
        }))
        self.assertIsNone(ip_quality_grade(None))

    def test_all_intelligence_failures_are_na(self):
        result = aggregate_ip_intelligence("203.0.113.30", {
            "ipqs": {"status": "timeout"},
            "scamalytics": {"status": "quota_unavailable"},
        })
        self.assertIsNone(result.ip_quality_score)
        self.assertIsNone(result.ip_grade)

    def test_high_fraud_score_drops_grade(self):
        score = compute_ip_quality_score({"ipqs": {"fraud_score": 96}})
        self.assertIsNotNone(score)
        self.assertLess(score, 40)
        self.assertEqual(ip_quality_grade(score), "D")

    def test_vendor_scores_are_not_averaged(self):
        score = compute_ip_quality_score({
            "ipqs": {"fraud_score": 96},
            "scamalytics": {"scamalytics_score": 5},
        })
        # A simple average would be around 50; worst-risk semantics remains D.
        self.assertLess(score, 40)

    def test_blacklist_and_abuse_are_deduplicated_facts(self):
        score = compute_ip_quality_score({
            "ipqs": {"recent_abuse": True, "frequent_abuser": True},
            "scamalytics": {"blacklisted": True, "is_blacklisted_external": True},
        })
        self.assertEqual(score, 17.0)
        self.assertEqual(compute_ip_grade(score), "D")

    def test_clean_explicit_risk_provider_can_grade(self):
        score = compute_ip_quality_score({
            "ipqs": {"fraud_score": 4, "recent_abuse": False,
                     "proxy": False, "vpn": False, "tor": False},
        })
        self.assertEqual(score, 100.0)
        self.assertEqual(ip_quality_grade(score), "S")


if __name__ == "__main__":
    unittest.main()
