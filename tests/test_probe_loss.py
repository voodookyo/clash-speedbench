# -*- coding: utf-8 -*-
"""Application-level probe success/failure accounting."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clash_speedbench as csb


class ProbeLossTest(unittest.TestCase):
    def test_failure_does_not_break_remaining_attempts(self):
        api = mock.Mock()
        api.proxy_delay.side_effect = [50, None, 60, None, 55]
        stats = csb.probe_latency(api, "node", 1000, count=5)
        self.assertEqual(api.proxy_delay.call_count, 5)
        self.assertEqual((stats.attempts, stats.successes, stats.failures), (5, 3, 2))
        self.assertEqual(stats.probe_success_rate, 60.0)
        self.assertEqual(stats.probe_loss_pct, 40.0)

    def test_ten_attempts_nine_success_is_ten_percent(self):
        api = mock.Mock()
        api.proxy_delay.side_effect = [100, 101, 99, 100, None, 98, 102, 100, 99, 101]
        stats = csb.probe_latency(api, "node", 1000, count=10)
        self.assertEqual(stats.attempts, 10)
        self.assertEqual(stats.successes, 9)
        self.assertEqual(stats.failures, 1)
        self.assertEqual(stats.loss_pct, 10.0)

    def test_default_and_stability_probe_count(self):
        self.assertEqual(csb._probe_count_from_args(SimpleNamespace()), 3)
        self.assertEqual(
            csb._probe_count_from_args(SimpleNamespace(stability=True)), 10
        )
        self.assertEqual(
            csb._probe_count_from_args(
                SimpleNamespace(stability=True, probe_count=7)
            ),
            7,
        )

    def test_result_serializes_application_metrics(self):
        result = csb.Result(
            name="node", provider="p", proto="ss", latency_ms=100,
            speeds_mbps=[20.0], median_mbps=20.0, best_mbps=20.0,
            status="ok", probe_attempts=10, probe_successes=9,
            probe_failures=1, probe_success_rate=90.0, probe_loss_pct=10.0,
        )
        payload = csb.result_to_dict(result)
        self.assertEqual(payload["probe_attempts"], 10)
        self.assertEqual(payload["probe_successes"], 9)
        self.assertEqual(payload["probe_failures"], 1)
        self.assertEqual(payload["probe_loss_pct"], 10.0)


if __name__ == "__main__":
    unittest.main()
