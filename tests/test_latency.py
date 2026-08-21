# -*- coding: utf-8 -*-
"""probe_latency：多次采样取中位数、jitter 为标准差、遇失败即停止。"""
import statistics
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clash_speedbench as csb


def fake_api(delays):
    """delays: proxy_delay 依序返回值（None 表示失败）。"""
    api = mock.Mock()
    api.proxy_delay.side_effect = list(delays)
    return api


class ProbeLatencyTest(unittest.TestCase):
    def test_median_of_odd_samples(self):
        api = fake_api([100, 120, 110])
        lat, jitter = csb.probe_latency(api, "节点A", 5000)
        self.assertEqual(lat, 110)
        self.assertAlmostEqual(jitter, 10.0)  # stdev([100,120,110]) = 10

    def test_median_of_even_samples(self):
        vals = [100, 200, 150, 180]
        api = fake_api(vals)
        lat, jitter = csb.probe_latency(api, "节点A", 5000, count=4)
        self.assertEqual(lat, 165)  # 排序 [100,150,180,200]，中位数 (150+180)/2
        self.assertAlmostEqual(jitter, round(statistics.stdev(vals), 1))

    def test_single_sample_jitter_zero(self):
        api = fake_api([88])
        lat, jitter = csb.probe_latency(api, "节点A", 5000, count=1)
        self.assertEqual(lat, 88)
        self.assertEqual(jitter, 0.0)

    def test_stops_at_first_failure(self):
        api = fake_api([100, None, 300])
        lat, jitter = csb.probe_latency(api, "节点A", 5000, count=3)
        self.assertEqual(lat, 100)          # 只采到第一个样本
        self.assertEqual(jitter, 0.0)
        self.assertEqual(api.proxy_delay.call_count, 2)  # 失败后不再继续

    def test_all_failed_returns_none(self):
        api = fake_api([None])
        lat, jitter = csb.probe_latency(api, "节点A", 5000)
        self.assertIsNone(lat)
        self.assertIsNone(jitter)
        self.assertEqual(api.proxy_delay.call_count, 1)

    def test_count_zero_still_probes_once(self):
        api = fake_api([77])
        lat, jitter = csb.probe_latency(api, "节点A", 5000, count=0)
        self.assertEqual(lat, 77)
        self.assertEqual(jitter, 0.0)

    def test_uses_delay_url_and_timeout(self):
        api = fake_api([50, 60, 70])
        csb.probe_latency(api, "节点B", 2500, count=3)
        for call in api.proxy_delay.call_args_list:
            self.assertEqual(call.args, ("节点B", csb.DEFAULT_DELAY_URL, 2500))

    def test_jitter_is_sample_stdev_rounded_1dp(self):
        vals = [200, 260, 220]
        api = fake_api(vals)
        _, jitter = csb.probe_latency(api, "节点A", 5000, count=3)
        self.assertEqual(jitter, round(statistics.stdev(vals), 1))


if __name__ == "__main__":
    unittest.main()
