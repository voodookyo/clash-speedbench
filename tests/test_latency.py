# -*- coding: utf-8 -*-
"""probe_latency：多次采样取中位数、jitter 为标准差并保留应用层失败率。"""
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

    def test_continues_after_failure_and_tracks_loss(self):
        api = fake_api([100, None, 300])
        stats = csb.probe_latency(api, "节点A", 5000, count=3)
        lat, jitter = stats
        self.assertEqual(lat, 200)          # 成功样本为 [100, 300]
        self.assertAlmostEqual(jitter, 141.4)
        self.assertEqual(api.proxy_delay.call_count, 3)
        self.assertEqual(stats.attempts, 3)
        self.assertEqual(stats.successes, 2)
        self.assertEqual(stats.failures, 1)
        self.assertAlmostEqual(stats.loss_pct, 33.3)

    def test_all_failed_returns_none(self):
        api = fake_api([None])
        stats = csb.probe_latency(api, "节点A", 5000)
        lat, jitter = stats
        self.assertIsNone(lat)
        self.assertIsNone(jitter)
        self.assertEqual(api.proxy_delay.call_count, 3)
        self.assertEqual(stats.attempts, 3)
        self.assertEqual(stats.successes, 0)
        self.assertEqual(stats.failures, 3)
        self.assertEqual(stats.loss_pct, 100.0)

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

    def test_ten_probes_nine_success_is_ten_percent_application_loss(self):
        api = fake_api([100, 101, None, 99, 102, 100, 98, 101, 100, 99])
        stats = csb.probe_latency(api, "节点A", 5000, count=10)
        self.assertEqual(stats.attempts, 10)
        self.assertEqual(stats.successes, 9)
        self.assertEqual(stats.failures, 1)
        self.assertEqual(stats.success_rate, 90.0)
        self.assertEqual(stats.loss_pct, 10.0)


if __name__ == "__main__":
    unittest.main()
