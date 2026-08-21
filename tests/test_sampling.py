# -*- coding: utf-8 -*-
"""adaptive_sample 带宽自适应档位/时限放宽 + multi_stream_speed 合计测试。"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clash_speedbench as csb


class AdaptiveSampleTest(unittest.TestCase):
    def test_unknown_rough_speed_defaults_10mb(self):
        self.assertEqual(csb.adaptive_sample(None, 4.0), (10, 4.0))

    def test_zero_or_negative_rough_speed_defaults_10mb(self):
        self.assertEqual(csb.adaptive_sample(0.0, 4.0), (10, 4.0))
        self.assertEqual(csb.adaptive_sample(-3.0, 4.0), (10, 4.0))

    def test_tier_boundaries(self):
        # 档位边界：<20→10MB、20~99.x→30MB、100~299.x→60MB、>=300→95MB
        self.assertEqual(csb.adaptive_sample(19.99, 4.0)[0], 10)
        self.assertEqual(csb.adaptive_sample(20.0, 4.0)[0], 30)
        self.assertEqual(csb.adaptive_sample(99.99, 4.0)[0], 30)
        self.assertEqual(csb.adaptive_sample(100.0, 4.0)[0], 60)
        self.assertEqual(csb.adaptive_sample(299.99, 4.0)[0], 60)
        self.assertEqual(csb.adaptive_sample(300.0, 4.0)[0], 95)
        self.assertEqual(csb.adaptive_sample(1000.0, 4.0)[0], 95)

    def test_max_time_relaxed_up_to_6s(self):
        # 10MB @ 5Mbps 预计 16s，*1.25=20s → 封顶 6s
        self.assertEqual(csb.adaptive_sample(5.0, 4.0), (10, 6.0))
        # 30MB @ 50Mbps 预计 4.8s，*1.25=6.0s → 6s
        self.assertEqual(csb.adaptive_sample(50.0, 4.0), (30, 6.0))
        # 30MB @ 40Mbps 预计 6s，*1.25=7.5s → 封顶 6s
        self.assertEqual(csb.adaptive_sample(40.0, 4.0), (30, 6.0))

    def test_max_time_never_below_base(self):
        # 30MB @ 80Mbps 预计 3s，*1.25=3.75s → 不小于 --max-time 4s
        self.assertEqual(csb.adaptive_sample(80.0, 4.0), (30, 4.0))
        # 95MB @ 1000Mbps 预计 0.76s → 仍保持 4s
        self.assertEqual(csb.adaptive_sample(1000.0, 4.0), (95, 4.0))

    def test_user_larger_max_time_wins(self):
        # 用户显式给了更大的 --max-time 时以用户为准
        self.assertEqual(csb.adaptive_sample(80.0, 10.0), (30, 10.0))
        self.assertEqual(csb.adaptive_sample(None, 10.0), (10, 10.0))

    def test_est_scaling(self):
        # 60MB @ 150Mbps 预计 3.2s *1.25 = 4.0s → max(4, 4)=4
        mb, mt = csb.adaptive_sample(150.0, 4.0)
        self.assertEqual(mb, 60)
        self.assertAlmostEqual(mt, 4.0)
        # 60MB @ 120Mbps 预计 4s *1.25 = 5s
        mb, mt = csb.adaptive_sample(120.0, 4.0)
        self.assertEqual(mb, 60)
        self.assertAlmostEqual(mt, 5.0)


class MultiStreamSpeedTest(unittest.TestCase):
    def patch_curl(self, results):
        """results: curl_speed 每次调用依序返回的 (mbps, status, connect_ms, size_mb)。"""
        return mock.patch.object(csb, "curl_speed", side_effect=list(results))

    def test_four_streams_summed(self):
        ok = (10.0, "ok", 100.0, 8.0)
        with self.patch_curl([ok] * 4) as m:
            total = csb.multi_stream_speed("http://127.0.0.1:7897", 8_000_000, 4.0, 3.0)
        self.assertEqual(total, 40.0)
        self.assertEqual(m.call_count, 4)

    def test_partial_failure_sums_successes(self):
        with self.patch_curl([(10.0, "ok", None, 8.0),
                              (None, "curl-7: x", None, 0.0),
                              (20.0, "ok", None, 8.0),
                              (None, "http-403", None, 0.0)]):
            total = csb.multi_stream_speed("http://127.0.0.1:7897", 8_000_000, 4.0, 3.0)
        self.assertEqual(total, 30.0)

    def test_all_failed_returns_none(self):
        fail = (None, "curl-timeout", None, 0.0)
        with self.patch_curl([fail] * 4):
            total = csb.multi_stream_speed("http://127.0.0.1:7897", 8_000_000, 4.0, 3.0)
        self.assertIsNone(total)

    def test_sum_rounded_to_3_decimals(self):
        ok = (10.1111, "ok", None, 8.0)
        with self.patch_curl([ok] * 4):
            total = csb.multi_stream_speed("http://127.0.0.1:7897", 8_000_000, 4.0, 3.0)
        self.assertEqual(total, round(10.1111 * 4, 3))

    def test_custom_stream_count(self):
        ok = (5.0, "ok", None, 8.0)
        with self.patch_curl([ok] * 2) as m:
            total = csb.multi_stream_speed("http://127.0.0.1:7897", 8_000_000, 4.0, 3.0,
                                           streams=2)
        self.assertEqual(total, 10.0)
        self.assertEqual(m.call_count, 2)


if __name__ == "__main__":
    unittest.main()
