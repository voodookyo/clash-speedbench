# -*- coding: utf-8 -*-
"""Network-only score remains independent from IP reputation."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clash_speedbench as csb


def result(**over):
    values = dict(
        name="node", provider="p", proto="ss", latency_ms=80,
        speeds_mbps=[100.0], median_mbps=100.0, best_mbps=100.0,
        status="ok",
    )
    values.update(over)
    return csb.Result(**values)


class NetworkScoreTest(unittest.TestCase):
    def test_bandwidth_is_a_prerequisite(self):
        r = result(median_mbps=None, speeds_mbps=[], best_mbps=None,
                    latency_ms=1, jitter_ms=0, connect_ms=1,
                    probe_attempts=10, probe_successes=10,
                    probe_failures=0, probe_success_rate=100)
        self.assertEqual(csb.compute_network_score(r), 0.0)
        self.assertEqual(csb.compute_score(r), 0.0)

    def test_ip_quality_cannot_rescue_missing_bandwidth(self):
        r = result(median_mbps=None, speeds_mbps=[], best_mbps=None)
        r.ip_quality_score = 100.0
        self.assertEqual(csb.compute_score(r), 0.0)

    def test_multi_stream_uses_400_mbps_full_scale(self):
        self.assertEqual(csb.multi_bandwidth_score(400), 100.0)
        self.assertEqual(csb.multi_bandwidth_score(200), 50.0)
        self.assertEqual(csb.multi_bandwidth_score(800), 100.0)

    def test_optional_dimensions_are_renormalized(self):
        # Only single-stream and latency are present: (35*100 + 20*100)/55.
        r = result()
        self.assertEqual(csb.compute_network_score(r), 100.0)
        r.latency_ms = 800
        self.assertEqual(csb.compute_network_score(r), round(35 * 100 / 55, 1))

    def test_jitter_and_connect_segments_are_monotonic(self):
        self.assertEqual(csb.jitter_score(5), 100.0)
        self.assertEqual(csb.jitter_score(200), 0.0)
        self.assertGreater(csb.jitter_score(20), csb.jitter_score(100))
        self.assertEqual(csb.connect_score(100), 100.0)
        self.assertEqual(csb.connect_score(2000), 0.0)
        self.assertGreater(csb.connect_score(200), csb.connect_score(1000))

    def test_probe_success_contributes_only_when_measured(self):
        r = result(latency_ms=None)
        no_probe = csb.compute_network_score(r)
        r.probe_attempts = 10
        r.probe_successes = 0
        r.probe_failures = 10
        r.probe_success_rate = 0.0
        r.probe_loss_pct = 100.0
        with_probe = csb.compute_network_score(r)
        self.assertLess(with_probe, no_probe)

    def test_ip_quality_is_optional_overall_component(self):
        r = result(latency_ms=80)
        network = csb.compute_network_score(r)
        self.assertEqual(csb.compute_score(r), network)
        r.ip_quality_score = 20.0
        self.assertEqual(csb.compute_score(r), round(network * 0.8 + 20 * 0.2, 1))


if __name__ == "__main__":
    unittest.main()
